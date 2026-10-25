#!/usr/bin/env python3
"""
r53_zone_to_cfn.py - put a Route 53 public hosted zone and every record in it,
the zone's own NS and SOA records included, into one CloudFormation stack: the
hosted zone plus one AWS::Route53::RecordSet per record, each its own resource.

Two change sets, because CloudFormation allows nothing shorter:
  1. IMPORT the hosted zone into a new stack       <zone>-1-import-zone.yml + .json
  2. UPDATE the stack: same template plus NS, SOA and every other record
                                                   <zone>-2-zone-and-records.yml
  * A hosted zone joins a stack only through an IMPORT change set, and during an
    import CloudFormation can't create, update or delete anything else.
  * Record sets can't be imported at all. When an update adds a record set that
    matches a live record exactly (name, type, TTL, values, alias and routing
    settings), CloudFormation takes over the live record instead of failing, so
    every value is copied verbatim from the Route 53 API. A record that doesn't
    match fails with "... but it already exists" and is left as it was.
  * Don't merge the steps with --import-existing-resources: it finds resources
    only by a custom name, and a hosted zone is known by its AWS-generated ID, so
    a CREATE change set would make a second hosted zone with the same name.

If a stack already manages the zone (for example after an IaC generator import),
step 1 is skipped: the deployed template is kept line for line and only the
records it doesn't have yet are appended.

Safety
  * Every resource the script writes has DeletionPolicy and UpdateReplacePolicy
    Retain: a rollback, a stack deletion or removing a resource never deletes DNS
    data. NS and SOA need it most, because Route 53 refuses to delete them.
  * Records another stack already manages are left out. The script reads the
    templates of the stacks in the current region and us-east-1 (see --regions).
  * A JSON backup of every record set is written before anything else.

Usage (AWS CloudShell, in the account and region for the stack)
  python3 r53_zone_to_cfn.py --zone-name tescoforbusiness.com
  python3 r53_zone_to_cfn.py --zone-name tescoforbusiness.com --types NS,SOA   # only those
  python3 r53_zone_to_cfn.py --zone-id Z0123456789ABCDEFGHIJ --only '^(www|_dmarc)\\.'
  python3 r53_zone_to_cfn.py --zone-name tescoforbusiness.com --exclude-ns-soa
  python3 r53_zone_to_cfn.py --from-file tescoforbusiness-com-backup-....json  # offline
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
EXTRA_REGION = 'us-east-1'   # where CloudFront and ACM stacks usually live

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
    """TemplateBody from get_template (a dict for JSON templates, a string otherwise)."""
    if isinstance(body, dict):
        return json.loads(json.dumps(body)), 'json'
    try:
        return json.loads(body), 'json'
    except ValueError:
        pass
    load = yaml_loader()
    if load is None:
        raise TemplateError('reading a YAML template needs PyYAML: pip3 install --user pyyaml')
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


def norm_name(name):
    """Record name for comparisons: lower case, no trailing dot, * instead of \\052."""
    return name.strip().lower().replace('\\052', '*').rstrip('.')


def record_key(name, rtype, set_id=''):
    return norm_name(name), rtype.strip().upper(), set_id or ''


def is_zone_own(rr, fqdn):
    """The NS and SOA records Route 53 created with the zone."""
    return rr['Name'].lower() == fqdn and rr['Type'] in ('NS', 'SOA')


def skip_reason(rr, fqdn, exclude_ns_soa):
    if exclude_ns_soa and is_zone_own(rr, fqdn):
        return "the zone's own record, left out by --exclude-ns-soa"
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
    """Readable, unique, alphanumeric ID, e.g. RecordWwwCNAME, RecordApexNS, RecordAppABlue."""
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


def describe(rr, fqdn):
    text = label(rr)
    if '\\052' in rr['Name']:
        text += " (\\052 is Route 53's escape for *)"
    if is_zone_own(rr, fqdn):
        text += " (the zone's own record: keep it Retain, Route 53 refuses to delete it)"
    if 'AliasTarget' in rr:
        text += f' -> alias {rr["AliasTarget"]["DNSName"]}'
    return text


def _resource(rtype, props):
    return {'Type': rtype, 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
            'Properties': props}


def build_records(chosen, fqdn, zone_ref, used):
    resources, comments, warnings = {}, {}, []
    for rr in chosen:
        lid = logical_id(rr, fqdn, used)
        resources[lid] = _resource('AWS::Route53::RecordSet', record_properties(rr, zone_ref))
        comments[lid] = describe(rr, fqdn)
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
    """(name, type, set id) of every record set a template declares, plus unreadable ones."""
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


def unprotected(resources):
    """Zone/record resources in a template that a stack deletion would delete."""
    kinds = ('AWS::Route53::HostedZone', 'AWS::Route53::RecordSet', 'AWS::Route53::RecordSetGroup')
    return [lid for lid, res in resources.items()
            if isinstance(res, dict) and res.get('Type') in kinds
            and res.get('DeletionPolicy') not in ('Retain', 'RetainExceptOnCreate')]


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


def describe_stack(cfn, name):
    try:
        return cfn.describe_stacks(StackName=name)['Stacks'][0]
    except Exception as exc:
        if 'does not exist' in str(exc):  # botocore ClientError: "Stack ... does not exist"
            return None
        raise


def template_resources(cfn, stack_id):
    """Resources of a stack's processed template, or None when it can't be read."""
    try:
        body = cfn.get_template(StackName=stack_id, TemplateStage='Processed')['TemplateBody']
        doc = parse_template(body)[0]
    except Exception:
        return None
    resources = doc.get('Resources') if isinstance(doc, dict) else None
    return resources if isinstance(resources, dict) else None


def _props(resources, lid):
    res = (resources or {}).get(lid)
    props = res.get('Properties') if isinstance(res, dict) else None
    return props if isinstance(props, dict) else {}


class _StackParams:
    """A stack's parameter values, fetched on first use."""

    def __init__(self, cfn, stack_id):
        self.cfn, self.stack_id, self.values = cfn, stack_id, None

    def get(self, key):
        if self.values is None:
            stack = describe_stack(self.cfn, self.stack_id) if self.cfn else None
            self.values = {p['ParameterKey']: p.get('ResolvedValue', p.get('ParameterValue'))
                           for p in (stack or {}).get('Parameters', [])}
        value = self.values.get(key)
        return value if isinstance(value, str) else None


def _resolve(value, zones, params):
    """A plain value, or a Ref/GetAtt that the stack itself answers; None otherwise."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        ref = value.get('Ref')
        if isinstance(ref, str):
            return zones[ref] if ref in zones else params.get(ref)
        att = value.get('Fn::GetAtt')
        if isinstance(att, list) and len(att) == 2 and att[1] == 'Id' and att[0] in zones:
            return zones[att[0]]
    return None


class StackScan:
    """Stacks in the given regions that already manage this hosted zone or any of its records."""

    def __init__(self, clients, zone, records):
        self.clients, self.zone = clients, zone
        self.names = {norm_name(r['Name']) for r in records}
        self.zone_owners = []   # (region, stack name, stack id, logical id)
        self.owners = {}        # record key -> {stack id: (label, sure)}
        self.name_owners = {}   # record name -> {stack id: label}, type unknown
        self.counts, self.warnings, self.unreadable = {}, [], 0

    def run(self, primary):
        for region, cfn in self.clients.items():
            try:
                stacks = [s for page in cfn.get_paginator('list_stacks').paginate()
                          for s in page['StackSummaries'] if s['StackStatus'] != 'DELETE_COMPLETE']
                for n, summary in enumerate(stacks, 1):
                    if n % 50 == 0:
                        print(f'  {region}: {n}/{len(stacks)} stacks', file=sys.stderr, flush=True)
                    try:
                        self._stack(region, cfn, summary['StackName'], summary['StackId'])
                    except Exception as exc:
                        if 'does not exist' not in str(exc):  # deleted while we looked: ignore
                            raise
                self.counts[region] = len(stacks)
            except Exception as exc:
                if region == primary:
                    raise
                self.warnings.append(f'could not check the stacks in {region} ({exc}); a record '
                                     'managed from there would not be noticed')
        if self.unreadable and yaml_loader() is None:
            self.warnings.append(f'{self.unreadable} stack template(s) could not be read without PyYAML, '
                                 'so records they might manage were left out: pip3 install --user pyyaml')
        return self

    def _stack(self, region, cfn, name, stack_id):
        live = [r for page in cfn.get_paginator('list_stack_resources').paginate(StackName=stack_id)
                for r in page['StackResourceSummaries'] if r.get('ResourceStatus') not in GONE]
        zones = {r['LogicalResourceId']: (r.get('PhysicalResourceId') or '').split('/')[-1]
                 for r in live if r['ResourceType'] == 'AWS::Route53::HostedZone'}
        self.zone_owners += [(region, name, stack_id, lid) for lid, pid in zones.items()
                             if pid == self.zone['id']]
        sets = {r['LogicalResourceId']: norm_name(r.get('PhysicalResourceId') or '')
                for r in live if r['ResourceType'] == 'AWS::Route53::RecordSet'}
        groups = [r['LogicalResourceId'] for r in live
                  if r['ResourceType'] == 'AWS::Route53::RecordSetGroup']
        if not sets and not groups:
            return
        tag = f'{name} ({region})'
        resources = template_resources(cfn, stack_id)
        readable = resources is not None
        if not readable:
            self.unreadable += 1
        params = _StackParams(cfn, stack_id)
        for lid, physical in sets.items():
            props = _props(resources, lid)
            names = {physical} | ({norm_name(props['Name'])} if isinstance(props.get('Name'), str) else set())
            if names & self.names:
                self._claim(stack_id, tag, names & self.names, props, props, zones, params, readable)
        for lid in groups:
            if not readable:
                self.warnings.append(f'stack {tag} has a record set group ({lid}) whose template could '
                                     'not be read; make sure it manages no record of this zone')
                continue
            group = _props(resources, lid)
            for rs in group.get('RecordSets') or []:
                rs = rs if isinstance(rs, dict) else {}
                if not isinstance(rs.get('Name'), str):
                    self.warnings.append(f'stack {tag}: record set group {lid} has a record without a '
                                         'plain Name; make sure it is not a record of this zone')
                    continue
                hits = {norm_name(rs['Name'])} & self.names
                own_zone = 'HostedZoneId' in rs or 'HostedZoneName' in rs
                if hits:
                    self._claim(stack_id, tag, hits, rs, rs if own_zone else group, zones, params, True)

    def _claim(self, stack_id, tag, names, props, zone_props, zones, params, readable):
        match = self._zone_match(zone_props, zones, params) if readable else None
        if match is False:
            return  # a record of another zone with the same name, e.g. a private zone
        rtype = _resolve(props.get('Type'), {}, params)
        set_id = _resolve(props.get('SetIdentifier'), {}, params) if 'SetIdentifier' in props else ''
        for name in names:
            if readable and isinstance(rtype, str) and isinstance(set_id, str):
                self.owners.setdefault(record_key(name, rtype, set_id), {})[stack_id] = (tag, match is True)
            else:
                self.name_owners.setdefault(name, {})[stack_id] = tag

    def _zone_match(self, props, zones, params):
        """True: this zone. False: another zone. None: can't tell."""
        zone_id = _resolve(props.get('HostedZoneId'), zones, params)
        if zone_id is not None:
            return zone_id.split('/')[-1] == self.zone['id']
        zone_name = _resolve(props.get('HostedZoneName'), {}, params)
        if zone_name is not None:
            return zone_fqdn(zone_name) == self.zone['fqdn']
        return None

    def others(self, rr, own_id):
        """(sure, maybe): other stacks that manage this record."""
        key = record_key(rr['Name'], rr['Type'], rr.get('SetIdentifier', ''))
        claims = [(tag, sure) for sid, (tag, sure) in self.owners.get(key, {}).items() if sid != own_id]
        claims += [(tag, False) for sid, tag in self.name_owners.get(key[0], {}).items() if sid != own_id]
        sure = sorted({tag for tag, is_sure in claims if is_sure})
        return sure, sorted({tag for tag, _ in claims} - set(sure))


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
        except Exception:
            region = None
    return region


def write_text(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2, default=str) + '\n')


# ------------------------------------------------------------------------ main

def _csv(text):
    return [part.strip() for part in text.split(',') if part.strip()]


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='Put a Route 53 public hosted zone and every record in it, NS and SOA '
                    'included, into one CloudFormation stack (IMPORT the zone, then UPDATE '
                    'with the records).',
        epilog='See the top of this file for why the zone import is its own change set.')
    parser.add_argument('--zone-name', help='public hosted zone name, e.g. tescoforbusiness.com')
    parser.add_argument('--zone-id', help='hosted zone ID (needed only if several zones share the name)')
    parser.add_argument('--stack-name', help='stack for the zone (default: <zone>-dns; a stack that '
                                             'already manages the zone is found automatically)')
    parser.add_argument('--prefix', help='file name prefix (default: zone name with dots as dashes)')
    parser.add_argument('--out-dir', default='.', help='where to write the files (default: here)')
    parser.add_argument('--only', metavar='REGEX', type=re.compile,
                        help='add only records whose name matches REGEX (rerun later for the rest)')
    parser.add_argument('--types', metavar='LIST', type=lambda s: {t.upper() for t in _csv(s)},
                        help='add only these record types, e.g. NS,SOA (rerun later for the rest)')
    parser.add_argument('--exclude-ns-soa', action='store_true',
                        help="leave out the zone's own NS and SOA records")
    parser.add_argument('--include-ns-soa', action='store_true', help=argparse.SUPPRESS)  # now the default
    parser.add_argument('--regions', metavar='LIST', type=_csv,
                        help='regions to search, besides the current one, for stacks that already '
                             f'manage the zone or its records (default: {EXTRA_REGION})')
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
    account, extras, backup, scan, clients, region = None, {}, None, None, {}, None

    if args.from_file:
        zone, records = load_backup(args.from_file, args.zone_id, args.zone_name)
    else:
        if boto3 is None:
            sys.exit('boto3 is not installed (AWS CloudShell has it): pip3 install boto3')
        region = current_region()
        if not region:
            sys.exit('No AWS region is set (CloudShell sets one). Run: export AWS_REGION=<region>')
        try:  # ride out API throttling while many stacks are read
            from botocore.config import Config
            config = Config(retries={'max_attempts': 10, 'mode': 'standard'})
        except ImportError:
            config = None
        account = boto3.client('sts').get_caller_identity()['Account']
        r53 = boto3.client('route53', config=config)
        zone = resolve_zone(r53, args.zone_name, args.zone_id)
        records = fetch_records(r53, zone['id'])
        regions = dict.fromkeys([region] + (EXTRA_REGION.split() if args.regions is None else args.regions))
        clients = {r: boto3.client('cloudformation', region_name=r, config=config) for r in regions}
        print(f'Looking for stacks that manage {zone["fqdn"]} or its records in '
              f'{", ".join(clients)} ...', file=sys.stderr, flush=True)
        try:
            scan = StackScan(clients, zone, records).run(region)
        except Exception as exc:
            sys.exit(f'Could not check the CloudFormation stacks in {region}: {exc}')

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
    stack_region, stack_name = region, args.stack_name or f'{base}-dns'
    zone_lid, stack, existing, own_id = ZONE_LOGICAL_ID, None, None, None
    if scan:
        owners = sorted(set(scan.zone_owners))
        if len(owners) > 1:
            sys.exit(f'{zone["fqdn"]} is in more than one stack: '
                     + ', '.join(f'{name} ({where})' for where, name, _, _ in owners)
                     + '. Sort that out before adding records.')
        if owners:
            stack_region, found, own_id, zone_lid = owners[0]
            if args.stack_name and args.stack_name != found:
                sys.exit(f'{zone["fqdn"]} is already in stack {found} ({stack_region}) as {zone_lid}. '
                         f'Rerun without --stack-name, or with --stack-name {found}.')
            stack_name, cfn = found, clients[stack_region]
            stack = describe_stack(cfn, own_id)
            status = stack['StackStatus']
            if not status.endswith('_COMPLETE') or status == 'ROLLBACK_COMPLETE':
                sys.exit(f'Stack {stack_name} is {status}, so it cannot take an update right now.')
            body = cfn.get_template(StackName=own_id, TemplateStage='Original')['TemplateBody']
            try:
                existing = (body,) + parse_template(body)
            except TemplateError as exc:
                sys.exit(f"Could not read stack {stack_name}'s template: {exc}")
        else:
            stack = describe_stack(clients[region], stack_name)
            if stack and stack['StackStatus'] == 'REVIEW_IN_PROGRESS':
                sys.exit(f'Stack {stack_name} is REVIEW_IN_PROGRESS: execute its import change set in the '
                         'console (or delete the stack to start over), then run this again.')
            if stack:
                sys.exit(f'Stack {stack_name} exists but does not manage {zone["fqdn"]}. '
                         'Pick another --stack-name; the script then imports the zone into a new stack.')
            extras = fetch_zone_extras(r53, zone['id'])

    # 3. Which records go in?
    already = set()
    if existing:
        already, unclear = managed_record_keys(existing[1].get('Resources', {}))
        if unclear:
            sys.exit(f'Stack {stack_name} has record sets whose Name/Type are not plain values '
                     f'({", ".join(unclear)}), so the script cannot tell which records they manage. '
                     'Add the remaining records by hand to avoid managing one record twice.')
    chosen, skipped, in_stack, unselected = [], [], 0, 0
    for rr in records:
        if record_key(rr['Name'], rr['Type'], rr.get('SetIdentifier', '')) in already:
            in_stack += 1
            continue
        reason = skip_reason(rr, zone['fqdn'], args.exclude_ns_soa)
        if not reason and ((args.only and not args.only.search(rr['Name']))
                           or (args.types and rr['Type'] not in args.types)):
            unselected += 1
            continue
        if not reason and scan:
            sure, maybe = scan.others(rr, own_id)
            if sure:
                reason = 'managed by stack ' + ', '.join(sure)
            elif maybe:
                reason = ('possibly managed by stack ' + ', '.join(maybe)
                          + " (its template doesn't show which zone or record type)")
        if reason:
            skipped.append((rr, reason))
        else:
            chosen.append(rr)
    own_records = sum(is_zone_own(rr, zone['fqdn']) for rr in chosen)

    # 4. Write the files.
    load = yaml_loader()
    source = args.from_file or 'the live zone'
    files, checks, warnings, resources, comments = [], [], [], {}, {}
    import_tpl = import_ids = records_tpl = params = None
    if existing is None:
        zdoc = zone_document(zone, extras)
        import_tpl, import_ids = out(f'{prefix}-1-import-zone.yml'), out(f'{prefix}-1-import-zone.json')
        write_text(import_tpl, render_template(zdoc, [
            f'Hosted zone {zone["fqdn"]} ({zone["id"]}): step 1 of 2, an IMPORT change set.',
            'An import can only bring in the zone; the records follow in step 2.',
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
                f'Hosted zone {zone["fqdn"]} ({zone["id"]}) with its record sets'
                + (", the zone's own NS and SOA included:" if own_records else ':'),
                'step 2 of 2, an UPDATE change set on the stack the zone was imported into.',
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
        print(f'Account     : {account}')
    if existing:
        print(f'Stack       : {stack_name} in {stack_region} ({stack["StackStatus"]}) already manages '
              f'the zone as {zone_lid}')
    elif scan:
        print(f'Stack       : {stack_name} in {stack_region} (new; the zone is not in any stack yet)')
    else:
        print('Stack       : not checked (--from-file): make sure no stack manages the zone or its '
              'records, and add zone tags or query logging to the step-1 file by hand')
    if scan:
        print('Checked     : ' + ', '.join(f'{n} stacks in {r}' for r, n in scan.counts.items()))
    read = f'{len(records)} record sets'
    if zone['count'] is not None and zone['count'] != len(records):
        read += f' (the zone reports {zone["count"]})'
    print(f'Read        : {read}' + (f', backed up to {backup}' if backup else f' from {args.from_file}'))
    if in_stack:
        print(f'Already in  : {in_stack} record set(s) already in {stack_name}')
    print(f'To add      : {len(chosen)}' + (f", the zone's NS and SOA among them" if own_records == 2 else ''))
    for rr, why in skipped:
        print(f'Left out    : {label(rr)} - {why}')
    if unselected:
        print(f'Left out    : {unselected} record set(s) not matching --only/--types (rerun to add them later)')
    if resources:
        print()
        width = max(len(lid) for lid in resources)
        for lid in resources:
            print(f'  {lid:<{width}}  {comments[lid]}')
    for warning in warnings + (scan.warnings if scan else []):
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
    where = f'      --region {stack_region} \\' if stack_region else None
    steps = ['', 'Next steps (in this directory)']
    if import_tpl:
        steps += ['  Step 1 - import the hosted zone into a new stack (an import can bring in nothing else):',
                  '    aws cloudformation create-change-set \\']
        steps += [where] if where else []
        steps += [
            f'      --stack-name {stack_name} \\',
            f'      --change-set-name import-hosted-zone-{stamp} \\',
            '      --change-set-type IMPORT \\',
            f'      --template-body file://{import_tpl} \\',
            f'      --resources-to-import file://{import_ids}',
            f'    Console > CloudFormation > {stack_name} (REVIEW_IN_PROGRESS) > Change sets: one',
            '    resource, action Import. Execute it and wait for IMPORT_COMPLETE.',
        ]
    if records_tpl:
        what = f'{len(chosen)} record set(s)' + (", the zone's NS and SOA among them," if own_records else '')
        steps += [f'  {"Step 2 - add" if import_tpl else "Add"} the {what} to {stack_name}:',
                  '    aws cloudformation create-change-set \\']
        steps += [where] if where else []
        steps += [
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
            '    file' + (' (or rerun with --exclude-ns-soa if it is NS or SOA)' if own_records else '')
            + ' and try again.',
            '  Optional checks first: aws cloudformation validate-template'
            + (f' --region {stack_region}' if stack_region else '') + f' --template-body file://{records_tpl}',
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
