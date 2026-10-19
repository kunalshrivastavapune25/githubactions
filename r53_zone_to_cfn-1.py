#!/usr/bin/env python3
"""
r53_zone_to_cfn.py - put a Route 53 public hosted zone and every record in it into
one CloudFormation stack: the hosted zone plus one AWS::Route53::RecordSet per
record, each its own resource.

It follows the two change sets that work by hand:
  1. IMPORT the hosted zone into a new stack    <zone>-1-import-zone.yml + .json
  2. UPDATE that stack with the same template plus every record
                                                <zone>-2-zone-and-records.yml
Record sets can't be imported. But when an update adds a record set that matches a
live record exactly (name, type, TTL, values, alias and routing settings),
CloudFormation takes over the live record instead of failing. So every value is
copied verbatim from the Route 53 API, trailing dots and TXT quoting included. A
record that doesn't match fails with "... but it already exists" and is left as it was.

If a stack already manages the zone (for example after an IaC generator import),
step 1 is skipped: the deployed template is kept exactly as it is and only the
records it doesn't have yet are appended. Records another stack manages are left
out, so no record ends up in two stacks.

Safety
  * Every resource the script writes has DeletionPolicy and UpdateReplacePolicy
    Retain, so a rollback, a stack deletion or removing a resource never deletes DNS.
  * The zone's own NS and SOA records stay out unless --include-ns-soa.
  * A JSON backup of every record set is written before anything else.

Usage (AWS CloudShell, in the account and region for the stack)
  python3 r53_zone_to_cfn.py --zone-name tescoforbusiness.com
  python3 r53_zone_to_cfn.py --zone-name tescoforbusiness.com --stack-name my-dns-stack
  python3 r53_zone_to_cfn.py --zone-id Z0123456789ABCDEFGHIJ --only '^(www|_dmarc)\\.'
  python3 r53_zone_to_cfn.py --from-file tescoforbusiness-com-backup-....json   # offline
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys

try:
    import boto3
except ImportError:  # only the live mode needs it; AWS CloudShell has it preinstalled
    boto3 = None

TEMPLATE_BODY_LIMIT = 51200  # bytes accepted by --template-body; bigger templates go via S3
ZONE_LOGICAL_ID = 'HostedZone'
GONE = ('DELETE_COMPLETE', 'DELETE_SKIPPED')

# ResourceRecordSet fields that AWS::Route53::RecordSet takes under the same name.
ROUTING_FIELDS = ('SetIdentifier', 'Weight', 'Region', 'Failover', 'GeoLocation',
                  'GeoProximityLocation', 'CidrRoutingConfig', 'MultiValueAnswer',
                  'HealthCheckId')
KNOWN_FIELDS = {'Name', 'Type', 'TTL', 'ResourceRecords', 'AliasTarget',
                'TrafficPolicyInstanceId', *ROUTING_FIELDS}


class TemplateError(Exception):
    pass


# ---------------------------------------------------------------- YAML writing
# A tiny emitter, so the script needs nothing beyond the Python standard library.

class Ref:
    """A CloudFormation Ref, written as `!Ref <name>`."""

    def __init__(self, name):
        self.name = name


_PLAIN = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]*$')
_TYPE_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9]*(::[A-Za-z0-9]+)+$')  # AWS::Route53::RecordSet
_YAML_WORDS = {'y', 'n', 'yes', 'no', 'true', 'false', 'on', 'off', 'null', '~'}


def yaml_scalar(value):
    if isinstance(value, Ref):
        return '!Ref ' + value.name
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if _TYPE_NAME.match(text) or (_PLAIN.match(text) and text.lower() not in _YAML_WORDS):
        return text
    # Single-quoted YAML keeps every character literally (backslashes, double quotes,
    # '#', ': '); only a single quote is doubled. Country code 'NO' stays a string.
    return "'" + text.replace("'", "''") + "'"


def yaml_lines(node, pad='', step='  '):
    lines = []
    if isinstance(node, dict):
        for key, val in node.items():
            if isinstance(val, (dict, list)):
                if not val:
                    raise ValueError(f'refusing to write an empty value for {key}')
                lines.append(f'{pad}{key}:')
                lines.extend(yaml_lines(val, pad + step, step))
            else:
                lines.append(f'{pad}{key}: {yaml_scalar(val)}')
    else:
        for item in node:
            if isinstance(item, dict):
                sub = yaml_lines(item, pad + '  ', step)  # keys line up after "- "
                sub[0] = f'{pad}- {sub[0][len(pad) + 2:]}'
                lines.extend(sub)
            else:
                lines.append(f'{pad}- {yaml_scalar(item)}')
    return lines


def resource_lines(resources, comments, pad, step='  '):
    lines = []
    for lid, body in resources.items():
        lines.append('')
        if lid in comments:
            lines.append(f'{pad}# {comments[lid]}')
        lines.extend(yaml_lines({lid: body}, pad, step))
    return lines


def render_template(doc, header, comments=None):
    out = [f'# {line}'.rstrip() for line in header] + ['']
    for key, val in doc.items():
        if key == 'Resources':
            out += ['', 'Resources:'] + resource_lines(val, comments or {}, '  ')[1:]
        elif isinstance(val, dict):
            out += ['', f'{key}:'] + yaml_lines(val, '  ')
        else:
            out.append(f'{key}: {yaml_scalar(val)}')
    return '\n'.join(out) + '\n'


def append_to_resources(text, resources, comments):
    """Insert resources at the end of the top-level Resources block; every other line stays as it is."""
    lines = text.lstrip('﻿').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    key = re.compile(r'''^(Resources|"Resources"|'Resources')\s*:\s*(#.*)?$''')
    start = next((i for i, line in enumerate(lines) if key.match(line)), None)
    if start is None:
        raise TemplateError('no top-level "Resources:" block found')
    end, pad, step = len(lines), None, None
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(lines[i]) - len(lines[i].lstrip(' '))
        if indent == 0:
            end = i
            break
        if pad is None:
            pad = ' ' * indent
        elif step is None and indent > len(pad):
            step = ' ' * (indent - len(pad))  # match the template's own nesting width
    if pad is None:
        raise TemplateError('the Resources block is empty')
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    tail = lines[at:]
    if tail and tail[0].strip():
        tail = [''] + tail
    merged = lines[:at] + resource_lines(resources, comments, pad, step or '  ') + tail
    return '\n'.join(merged).rstrip('\n') + '\n'


# ---------------------------------------------------------------- YAML reading

def yaml_loader():
    """A loader for CloudFormation YAML (short-form tags included), or None without PyYAML."""
    try:
        import yaml
    except ImportError:
        return None

    class Loader(yaml.SafeLoader):
        pass

    def intrinsic(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        if suffix in ('Ref', 'Condition'):
            return {suffix: value}
        if suffix == 'GetAtt' and isinstance(value, str):
            value = value.split('.', 1)
        return {'Fn::' + suffix: value}

    Loader.add_multi_constructor('!', intrinsic)
    return lambda text: yaml.load(text, Loader=Loader)


def parse_template(body):
    """TemplateBody from get_template: a dict for JSON templates, a string otherwise."""
    if isinstance(body, dict):
        return json.loads(json.dumps(body)), 'json'
    try:
        return json.loads(body), 'json'
    except ValueError:
        pass
    load = yaml_loader()
    if load is None:
        sys.exit("Reading the stack's YAML template needs PyYAML: pip3 install --user pyyaml")
    return load(body), 'yaml'


def _as_plain(node):
    if isinstance(node, Ref):
        return {'Ref': node.name}
    if isinstance(node, dict):
        return {k: _as_plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_as_plain(v) for v in node]
    return node


def verify(original, text, added, load):
    """Re-read the written template: it must be `original` plus exactly `added`, nothing else."""
    if load is None:
        return 'skipped (PyYAML not installed)'
    merged = load(text)
    problems = []
    if set(merged) != set(original):
        problems.append('top-level sections differ')
    problems += [f'{key} changed' for key in original
                 if key != 'Resources' and merged.get(key) != original[key]]
    old, new = original.get('Resources', {}), merged.get('Resources', {})
    problems += [f'existing resource {lid} changed' for lid in old if new.get(lid) != old[lid]]
    if {lid: res for lid, res in new.items() if lid not in old} != _as_plain(added):
        problems.append('the new resources differ from the live records')
    if problems:
        sys.exit('Self-check FAILED: ' + '; '.join(problems) + '. Do not deploy the generated file.')
    return 'passed'


# --------------------------------------------------------------------- records

def zone_fqdn(name):
    return name.strip().lower().rstrip('.') + '.'


def record_key(name, rtype, set_id=''):
    name = name.strip().lower().replace('\\052', '*')
    return (name if name.endswith('.') else name + '.', rtype.upper(), set_id or '')


def skip_reason(rr, fqdn, include_ns_soa):
    if rr['Name'].lower() == fqdn and rr['Type'] in ('NS', 'SOA') and not include_ns_soa:
        return "the zone's own record (add --include-ns-soa to manage it too)"
    if 'TrafficPolicyInstanceId' in rr:
        return 'owned by a traffic policy instance'
    if 'AliasTarget' not in rr and ('TTL' not in rr or not rr.get('ResourceRecords')):
        return 'has no alias target and no TTL/values'
    return None


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, '', [], {})}
    return value


def record_properties(rr, zone_ref):
    props = {'HostedZoneId': Ref(zone_ref), 'Name': rr['Name'], 'Type': rr['Type']}
    for field in ROUTING_FIELDS:
        if field in rr:
            props[field] = _clean(rr[field])
    if 'AliasTarget' in rr:
        alias = rr['AliasTarget']
        props['AliasTarget'] = {
            'HostedZoneId': alias['HostedZoneId'],
            'DNSName': alias['DNSName'],
            'EvaluateTargetHealth': alias.get('EvaluateTargetHealth', False),
        }
    else:
        props['TTL'] = str(rr['TTL'])
        props['ResourceRecords'] = [r['Value'] for r in rr['ResourceRecords']]
    return props


def _camel(text):
    return ''.join(w[:1].upper() + w[1:] for w in re.split(r'[^A-Za-z0-9]+', text) if w)


def logical_id(rr, fqdn, used):
    """Readable, unique, alphanumeric ID, e.g. RecordWwwCNAME, RecordApexMX, RecordAppABlue."""
    name = rr['Name'].lower()
    if name == fqdn:
        labels = []
    elif name.endswith('.' + fqdn):
        labels = name[:-len(fqdn) - 1].split('.')
    else:
        labels = name.rstrip('.').split('.')
    words = ['Wildcard' if label in ('*', '\\052') else (_camel(label) or 'X') for label in labels]
    base = 'Record' + (''.join(words) or 'Apex') + re.sub(r'[^A-Za-z0-9]', '', rr['Type'])
    if rr.get('SetIdentifier'):
        base += _camel(rr['SetIdentifier']) or 'Set'
    if len(base) > 200:
        key = f"{rr['Name']}|{rr['Type']}|{rr.get('SetIdentifier', '')}"
        base = base[:190] + hashlib.sha1(key.encode()).hexdigest()[:8]
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f'{base}{n}', n + 1
    used.add(candidate)
    return candidate


def label(rr):
    text = f'{rr["Name"]} {rr["Type"]}'
    return text + (f' [{rr["SetIdentifier"]}]' if rr.get('SetIdentifier') else '')


def describe(rr):
    text = label(rr)
    if '\\052' in rr['Name']:
        text += " (\\052 is Route 53's escape for *)"
    if 'AliasTarget' in rr:
        text += f' -> alias {rr["AliasTarget"]["DNSName"]}'
    return text


def unprotected(resources):
    """Zone/record resources in a template that a stack deletion would delete."""
    kinds = ('AWS::Route53::HostedZone', 'AWS::Route53::RecordSet', 'AWS::Route53::RecordSetGroup')
    return [lid for lid, res in resources.items()
            if isinstance(res, dict) and res.get('Type') in kinds
            and res.get('DeletionPolicy') not in ('Retain', 'RetainExceptOnCreate')]


def _resource(rtype, props):
    return {'Type': rtype, 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
            'Properties': props}


def build_records(chosen, fqdn, zone_ref, used):
    resources, comments, warnings = {}, {}, []
    for rr in chosen:
        lid = logical_id(rr, fqdn, used)
        resources[lid] = _resource('AWS::Route53::RecordSet', record_properties(rr, zone_ref))
        comments[lid] = describe(rr)
        unknown = sorted(set(rr) - KNOWN_FIELDS)
        if unknown:
            warnings.append(f'{lid}: field(s) {", ".join(unknown)} not mapped, so CloudFormation may '
                            'not match this record. Remove it from the file or handle it by hand.')
    return resources, comments, warnings


def zone_document(zone, extras):
    props = {'Name': zone['fqdn']}
    if zone.get('comment'):
        props['HostedZoneConfig'] = {'Comment': zone['comment']}
    if extras.get('tags'):
        props['HostedZoneTags'] = [{'Key': t['Key'], 'Value': t['Value']} for t in extras['tags']]
    if extras.get('query_log_arn'):
        props['QueryLoggingConfig'] = {'CloudWatchLogsLogGroupArn': extras['query_log_arn']}
    return {
        'AWSTemplateFormatVersion': '2010-09-09',
        'Description': f'Public hosted zone {zone["name"]} and its record sets',
        'Resources': {ZONE_LOGICAL_ID: _resource('AWS::Route53::HostedZone', props)},
    }


def managed_record_keys(resources):
    """(name, type, set id) of every record set a template already declares, plus unreadable ones."""
    keys, unclear = set(), set()
    for lid, res in resources.items():
        if not isinstance(res, dict):
            continue
        props = res.get('Properties') or {}
        if res.get('Type') == 'AWS::Route53::RecordSet':
            sets = [props]
        elif res.get('Type') == 'AWS::Route53::RecordSetGroup':
            sets = props.get('RecordSets')
            if not isinstance(sets, list):
                unclear.add(lid)
                continue
        else:
            continue
        for rs in sets:
            rs = rs if isinstance(rs, dict) else {}
            name, rtype, sid = rs.get('Name'), rs.get('Type'), rs.get('SetIdentifier') or ''
            if isinstance(name, str) and isinstance(rtype, str) and isinstance(sid, str):
                keys.add(record_key(name, rtype, sid))
            else:
                unclear.add(lid)
    return keys, sorted(unclear)


# ------------------------------------------------------------------------- AWS

def resolve_zone(r53, zone_name, zone_id):
    if zone_id:
        hz = r53.get_hosted_zone(Id=zone_id.split('/')[-1])['HostedZone']
    else:
        fqdn = zone_fqdn(zone_name)
        found, kwargs = [], {'DNSName': fqdn}
        while True:
            page = r53.list_hosted_zones_by_name(**kwargs)
            found += [z for z in page['HostedZones'] if z['Name'].lower() == fqdn]
            if not page.get('IsTruncated') or page.get('NextDNSName', '').lower() != fqdn:
                break
            kwargs = {'DNSName': page['NextDNSName'], 'HostedZoneId': page['NextHostedZoneId']}
        public = [z for z in found if not z['Config'].get('PrivateZone')]
        if not public:
            sys.exit(f'No public hosted zone named {fqdn} in this account.')
        if len(public) > 1:
            ids = ', '.join(z['Id'].split('/')[-1] for z in public)
            sys.exit(f'{len(public)} public hosted zones are named {fqdn} ({ids}); rerun with --zone-id.')
        hz = public[0]
    if hz['Config'].get('PrivateZone'):
        sys.exit(f'{hz["Name"]} is a private hosted zone; this script is for public zones.')
    return {
        'id': hz['Id'].split('/')[-1],
        'fqdn': zone_fqdn(hz['Name']),
        'name': zone_fqdn(hz['Name']).rstrip('.'),
        'comment': hz['Config'].get('Comment', ''),
        'count': hz.get('ResourceRecordSetCount'),
    }


def fetch_records(r53, zone_id):
    records = []
    for page in r53.get_paginator('list_resource_record_sets').paginate(HostedZoneId=zone_id):
        records.extend(page['ResourceRecordSets'])
    return records


def fetch_zone_extras(r53, zone_id):
    tags = r53.list_tags_for_resource(ResourceType='hostedzone', ResourceId=zone_id)
    logs = r53.list_query_logging_configs(HostedZoneId=zone_id).get('QueryLoggingConfigs', [])
    return {
        'tags': tags.get('ResourceTagSet', {}).get('Tags', []),
        'query_log_arn': logs[0]['CloudWatchLogsLogGroupArn'] if logs else None,
    }


def _missing(exc):
    return 'does not exist' in str(exc)  # botocore ClientError: "Stack ... does not exist"


def describe_stack(cfn, name):
    try:
        return cfn.describe_stacks(StackName=name)['Stacks'][0]
    except Exception as exc:
        if _missing(exc):
            return None
        raise


class StackIndex:
    """Finds the live stack, if any, that holds a given physical resource ID."""

    def __init__(self, cfn):
        self.cfn, self.cache = cfn, {}

    def holders(self, physical_id, rtype):
        try:
            found = self.cfn.describe_stack_resources(PhysicalResourceId=physical_id)['StackResources']
        except Exception as exc:
            if _missing(exc):
                return []
            raise
        if not found:
            return []
        name, stack_id = found[0]['StackName'], found[0]['StackId']
        if stack_id not in self.cache:  # list everything: DescribeStackResources stops at 100
            items = []
            for page in self.cfn.get_paginator('list_stack_resources').paginate(StackName=stack_id):
                items += page['StackResourceSummaries']
            self.cache[stack_id] = items
        return [(name, r['LogicalResourceId']) for r in self.cache[stack_id]
                if r['ResourceType'] == rtype and r.get('ResourceStatus') not in GONE
                and (r.get('PhysicalResourceId') or '').split('/')[-1] == physical_id]

    def record_owners(self, rr):
        base = rr['Name'].rstrip('.')
        variants = {base, base + '.'}
        if '\\052' in base:
            star = base.replace('\\052', '*')
            variants |= {star, star + '.'}
        return {stack for pid in sorted(variants)
                for stack, _ in self.holders(pid, 'AWS::Route53::RecordSet')}


def load_backup(path, zone_id, zone_name):
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    if isinstance(data, list):
        data = {'ResourceRecordSets': data}
    zone_id = (zone_id or data.get('HostedZoneId') or '').split('/')[-1]
    zone_name = zone_name or data.get('HostedZoneName')
    if not zone_id or not zone_name:
        sys.exit('--from-file needs --zone-id and --zone-name unless the file is a backup from this script.')
    fqdn = zone_fqdn(zone_name)
    zone = {'id': zone_id, 'fqdn': fqdn, 'name': fqdn.rstrip('.'),
            'comment': data.get('HostedZoneComment', ''), 'count': None}
    return zone, data['ResourceRecordSets']


def current_region():
    region = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION')
    if not region and boto3 is not None:
        try:
            region = boto3.session.Session().region_name
        except Exception:  # informational only
            region = None
    return region or 'not set'


def write_text(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2, default=str) + '\n')


# ------------------------------------------------------------------------ main

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='Put a Route 53 public hosted zone and every record in it into one '
                    'CloudFormation stack (IMPORT the zone, then UPDATE with the records).',
        epilog='See the top of this file for how the records get adopted.')
    parser.add_argument('--zone-name', help='public hosted zone name, e.g. tescoforbusiness.com')
    parser.add_argument('--zone-id', help='hosted zone ID (needed only if several zones share the name)')
    parser.add_argument('--stack-name', help='stack for the zone (default: <zone>-dns; a stack that '
                                             'already manages the zone is found automatically)')
    parser.add_argument('--prefix', help='file name prefix (default: zone name with dots as dashes)')
    parser.add_argument('--out-dir', default='.', help='where to write the files (default: here)')
    parser.add_argument('--only', metavar='REGEX', type=re.compile,
                        help='add only records whose name matches REGEX (rerun later for the rest)')
    parser.add_argument('--include-ns-soa', action='store_true',
                        help="also manage the zone's own NS and SOA records")
    parser.add_argument('--from-file', metavar='JSON',
                        help='read records from a saved backup or list-resource-record-sets output')
    args = parser.parse_args(argv)
    if not (args.zone_name or args.zone_id or args.from_file):
        parser.error('give --zone-name or --zone-id')
    return args


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    captured_at, stamp = now.strftime('%Y-%m-%dT%H:%M:%SZ'), now.strftime('%Y%m%d-%H%M%S')
    account, extras, backup, index, cfn = None, {}, None, None, None

    if args.from_file:
        zone, records = load_backup(args.from_file, args.zone_id, args.zone_name)
    else:
        if boto3 is None:
            sys.exit('boto3 is not installed (AWS CloudShell has it): pip3 install boto3')
        account = boto3.client('sts').get_caller_identity()['Account']
        r53, cfn = boto3.client('route53'), boto3.client('cloudformation')
        index = StackIndex(cfn)
        zone = resolve_zone(r53, args.zone_name, args.zone_id)
        records = fetch_records(r53, zone['id'])

    base = re.sub(r'[^a-z0-9-]', '-', zone['name'])
    if not base[:1].isalpha():
        base = 'zone-' + base
    prefix = args.prefix or base
    os.makedirs(args.out_dir, exist_ok=True)

    def out(name):
        return os.path.normpath(os.path.join(args.out_dir, name))

    # 1. Back up the zone before anything else.
    if not args.from_file:
        backup = out(f'{base}-backup-{now:%Y%m%dT%H%M%SZ}.json')
        write_json(backup, {'CapturedAt': captured_at, 'Account': account,
                            'HostedZoneId': zone['id'], 'HostedZoneName': zone['fqdn'],
                            'HostedZoneComment': zone['comment'], 'ResourceRecordSets': records})

    # 2. New stack, or one that already manages the zone?
    stack_name, zone_lid, stack, existing = args.stack_name or f'{base}-dns', ZONE_LOGICAL_ID, None, None
    if index:
        owner = index.holders(zone['id'], 'AWS::Route53::HostedZone')
        if owner:
            if args.stack_name and args.stack_name != owner[0][0]:
                sys.exit(f'{zone["fqdn"]} is already in stack {owner[0][0]} (as {owner[0][1]}). '
                         f'Rerun without --stack-name, or with --stack-name {owner[0][0]}.')
            stack_name, zone_lid = owner[0]
            stack = describe_stack(cfn, stack_name)
            status = stack['StackStatus']
            if not status.endswith('_COMPLETE') or status == 'ROLLBACK_COMPLETE':
                sys.exit(f'Stack {stack_name} is {status}, so it cannot take an update right now.')
            body = cfn.get_template(StackName=stack_name, TemplateStage='Original')['TemplateBody']
            existing = (body,) + parse_template(body)
        else:
            stack = describe_stack(cfn, stack_name)
            if stack and stack['StackStatus'] == 'REVIEW_IN_PROGRESS':
                sys.exit(f'Stack {stack_name} is REVIEW_IN_PROGRESS: execute its import change set in the '
                         'console (or delete the stack to start over), then run this again.')
            if stack:
                sys.exit(f'Stack {stack_name} exists but does not manage {zone["fqdn"]}. '
                         'Pick another --stack-name; the script then imports the zone into a new stack.')
            extras = fetch_zone_extras(r53, zone['id'])

    # 3. Which records go in?
    already, unclear = set(), []
    if existing:
        already, unclear = managed_record_keys(existing[1].get('Resources', {}))
        if unclear:
            sys.exit(f'Stack {stack_name} has record sets whose Name/Type are not plain values '
                     f'({", ".join(unclear)}), so the script cannot tell which records they manage. '
                     'Add the remaining records by hand to avoid managing one record twice.')
    chosen, skipped, in_stack, unselected = [], [], 0, 0
    for rr in records:
        reason = skip_reason(rr, zone['fqdn'], args.include_ns_soa)
        if not reason and record_key(rr['Name'], rr['Type'], rr.get('SetIdentifier', '')) in already:
            in_stack += 1
            continue
        if not reason and args.only and not args.only.search(rr['Name']):
            unselected += 1
            continue
        if not reason and index:
            owners = index.record_owners(rr)
            if stack_name in owners:
                in_stack += 1
                continue
            if owners:
                reason = 'managed by stack ' + ', '.join(sorted(owners))
        if reason:
            skipped.append((rr, reason))
        else:
            chosen.append(rr)

    # 4. Write the files.
    load = yaml_loader()
    source = args.from_file or 'the live zone'
    files, checks, warnings, resources = [], [], [], {}
    import_tpl = import_ids = records_tpl = params = None
    if existing is None:
        zdoc = zone_document(zone, extras)
        import_tpl, import_ids = out(f'{prefix}-1-import-zone.yml'), out(f'{prefix}-1-import-zone.json')
        write_text(import_tpl, render_template(zdoc, [
            f'Hosted zone {zone["fqdn"]} ({zone["id"]}): step 1 of 2, an IMPORT change set.',
            f'Generated by r53_zone_to_cfn.py from {source} at {captured_at}.',
            'DeletionPolicy Retain: deleting the stack never deletes the hosted zone.']))
        write_json(import_ids, [{'ResourceType': 'AWS::Route53::HostedZone',
                                 'LogicalResourceId': ZONE_LOGICAL_ID,
                                 'ResourceIdentifier': {'Id': zone['id']}}])
        files += [import_tpl, import_ids]
        if load:
            if load(open(import_tpl, encoding='utf-8').read()) != _as_plain(zdoc):
                sys.exit(f'Self-check FAILED: {import_tpl} does not read back as intended. Do not deploy it.')
        checks.append(f'{import_tpl}: ' + ('passed' if load else 'skipped (PyYAML not installed)'))
    if chosen:
        if existing is None:
            header = [
                f'Hosted zone {zone["fqdn"]} ({zone["id"]}) and its record sets: step 2 of 2,',
                'an UPDATE change set on the stack the zone was imported into.',
                f'Generated by r53_zone_to_cfn.py from {source} at {captured_at}.',
                '',
                'Record values are copied verbatim from Route 53. CloudFormation takes over an',
                'existing record only when the template matches it exactly, so deploy this file',
                'unchanged the first time. Every resource is Retain: deleting the stack or',
                'removing a resource keeps the DNS data.',
            ]
            if skipped:
                header.append('Left out: ' + ', '.join(label(rr) for rr, _ in skipped))
            original_text, original, fmt = render_template(zdoc, header), _as_plain(zdoc), 'yaml'
        else:
            original_text, original, fmt = existing
        resources, comments, warnings = build_records(chosen, zone['fqdn'], zone_lid,
                                                      set(original.get('Resources', {})))
        records_tpl = out(f'{prefix}-2-zone-and-records.' + ('json' if fmt == 'json' else 'yml'))
        if fmt == 'json':
            merged = dict(original, Resources=dict(original.get('Resources', {}), **_as_plain(resources)))
            write_text(records_tpl, json.dumps(merged, indent=2) + '\n')
            checks.append(f'{records_tpl}: ' + verify(original, open(records_tpl).read(), resources, json.loads))
        else:
            try:
                write_text(records_tpl, append_to_resources(original_text, resources, comments))
            except TemplateError as exc:
                sys.exit(f"Could not extend stack {stack_name}'s template: {exc}.")
            checks.append(f'{records_tpl}: ' + verify(original, open(records_tpl).read(), resources, load))
        files.append(records_tpl)
        if stack and stack.get('Parameters'):
            params = out(f'{prefix}-2-params.json')
            write_json(params, [{'ParameterKey': p['ParameterKey'], 'UsePreviousValue': True}
                                for p in stack['Parameters']])
            files.append(params)

    # 5. Report.
    print(f'Hosted zone : {zone["fqdn"]}  {zone["id"]}')
    if account:
        print(f'Account     : {account}   (CloudFormation region {current_region()})')
    if existing:
        print(f'Stack       : {stack_name} ({stack["StackStatus"]}) already manages the zone as {zone_lid}')
    elif index:
        print(f'Stack       : {stack_name} (new; the zone is not in any stack yet)')
    else:
        print('Stack       : not checked (--from-file): make sure no stack manages the zone or its '
              'records, and add zone tags or query logging to the step-1 file by hand')
    read = f'{len(records)} record sets'
    if zone['count'] is not None and zone['count'] != len(records):
        read += f' (the zone reports {zone["count"]})'
    print(f'Read        : {read}' + (f', backed up to {backup}' if backup else f' from {args.from_file}'))
    if in_stack:
        print(f'Already in  : {in_stack} record set(s) already in {stack_name}')
    print(f'To add      : {len(chosen)}')
    for rr, why in skipped:
        print(f'Left out    : {label(rr)} - {why}')
    if unselected:
        print(f'Left out    : {unselected} record set(s) not matching --only (rerun to add them later)')
    if resources:
        print()
        width = max(len(lid) for lid in resources)
        for lid in resources:
            print(f'  {lid:<{width}}  {comments[lid]}')
    for warning in warnings:
        print(f'WARNING     : {warning}')
    exposed = unprotected(existing[1].get('Resources', {})) if existing else []
    if exposed:
        print(f'WARNING     : already in {stack_name} without DeletionPolicy Retain: {", ".join(exposed)}. '
              'Deleting the stack would delete them from Route 53; consider adding Retain in a later update.')
    print()
    if files:
        print('Wrote       : ' + ', '.join(files))
    for check in checks:
        print(f'Self-check  : {check}')
    for path in (import_tpl, records_tpl):
        if path and os.path.getsize(path) > TEMPLATE_BODY_LIMIT:
            print(f'NOTE        : {path} is over {TEMPLATE_BODY_LIMIT:,} bytes; upload it to S3 and use '
                  '--template-url instead of --template-body.')

    if not chosen and existing:
        print(f'\nNothing to add: {stack_name} already has every record this run would manage.')
        return
    steps = ['', 'Next steps (in this directory)']
    if import_tpl:
        steps += [
            '  Step 1 - import the hosted zone into a new stack:',
            '    aws cloudformation create-change-set \\',
            f'      --stack-name {stack_name} \\',
            f'      --change-set-name import-hosted-zone-{stamp} \\',
            '      --change-set-type IMPORT \\',
            f'      --template-body file://{import_tpl} \\',
            f'      --resources-to-import file://{import_ids}',
            f'    Console > CloudFormation > {stack_name} (REVIEW_IN_PROGRESS) > Change sets: one',
            '    resource, action Import. Execute it and wait for IMPORT_COMPLETE.',
        ]
    if records_tpl:
        steps += [
            f'  {"Step 2 - add" if import_tpl else "Add"} the {len(chosen)} record set(s) to {stack_name}:',
            '    aws cloudformation create-change-set \\',
            f'      --stack-name {stack_name} \\',
            f'      --change-set-name add-records-{stamp} \\',
            '      --change-set-type UPDATE \\',
            f'      --template-body file://{records_tpl}' + (' \\' if params else ''),
        ]
        if params:
            steps.append(f'      --parameters file://{params}')
        steps += [
            '    The change set must show only Add actions, one per record, and nothing for the',
            '    hosted zone. Execute it. If a record fails to match, the event names it and the',
            '    stack rolls back without deleting anything (Retain): remove that record from the',
            '    file and try again.',
            f'  Optional checks first: aws cloudformation validate-template --template-body file://{records_tpl}',
            f'    and: pip3 install --user cfn-lint && ~/.local/bin/cfn-lint {records_tpl}',
        ]
        if backup:
            steps += [
                '  Afterwards, confirm Route 53 itself did not change:',
                f'    diff <(jq -S .ResourceRecordSets {backup}) \\',
                f'         <(aws route53 list-resource-record-sets --hosted-zone-id {zone["id"]} '
                '| jq -S .ResourceRecordSets) && echo "zone unchanged"',
            ]
    print('\n'.join(steps))


if __name__ == '__main__':
    main()
