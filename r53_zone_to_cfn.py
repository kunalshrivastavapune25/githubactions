#!/usr/bin/env python3
"""
r53_zone_to_cfn.py - put the hand-made records of a Route 53 public hosted zone
under CloudFormation.

Reads the live zone with boto3 and writes:
  <prefix>-records.yml          template, one AWS::Route53::RecordSet per record
  <prefix>-records-params.json  parameters file for `aws cloudformation create-change-set`
  <zone>-backup-<UTC>.json      every record set exactly as Route 53 returned it
  with --zone-import:           <zone>-zone.yml + <zone>-zone-import.json, to IMPORT
                                the hosted zone itself into its own stack

Why the records change set is CREATE, not IMPORT
  AWS::Route53::RecordSet is not on CloudFormation's import-supported list, so an
  IMPORT change set containing record sets is rejected. When CloudFormation creates
  a record set that already exists with exactly the same name, type, TTL, values and
  alias/routing settings, it takes over the live record instead of failing. This is
  undocumented but widely reported, and AWS Support has suggested it as the
  workaround. If anything differs, that resource fails with "... but it already
  exists" and the live record is left as it was. That is why every value below is
  copied verbatim from the Route 53 API, trailing dots and TXT quoting included.

Safety
  * DeletionPolicy and UpdateReplacePolicy are Retain on every resource, so a
    rollback, a stack deletion or removing a resource never deletes DNS data.
  * The zone's own NS and SOA records are left out (they belong to the hosted zone),
    and so are records owned by a traffic policy.

Usage (AWS CloudShell, in the account that owns the zone)
  python3 r53_zone_to_cfn.py --zone-name tescoforbusiness.com
  python3 r53_zone_to_cfn.py --zone-id Z0123456789ABCDEFGHIJ
  python3 r53_zone_to_cfn.py --zone-name tescoforbusiness.com --only '^_dmarc\\.'  # trial stack, matching records only
  python3 r53_zone_to_cfn.py --zone-name tescoforbusiness.com --zone-import       # also IMPORT files for the zone
  python3 r53_zone_to_cfn.py --from-file tescoforbusiness-com-backup-....json     # offline, from a saved backup
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

# ResourceRecordSet fields that AWS::Route53::RecordSet takes under the same name.
ROUTING_FIELDS = ('SetIdentifier', 'Weight', 'Region', 'Failover', 'GeoLocation',
                  'GeoProximityLocation', 'CidrRoutingConfig', 'MultiValueAnswer',
                  'HealthCheckId')
KNOWN_FIELDS = {'Name', 'Type', 'TTL', 'ResourceRecords', 'AliasTarget',
                'TrafficPolicyInstanceId', *ROUTING_FIELDS}


# --------------------------------------------------------------------------- YAML
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


def yaml_lines(node, indent):
    pad = '  ' * indent
    lines = []
    if isinstance(node, dict):
        for key, val in node.items():
            if isinstance(val, (dict, list)):
                if not val:
                    raise ValueError(f'refusing to write an empty value for {key}')
                lines.append(f'{pad}{key}:')
                lines.extend(yaml_lines(val, indent + 1))
            else:
                lines.append(f'{pad}{key}: {yaml_scalar(val)}')
    else:
        for item in node:
            if isinstance(item, dict):
                sub = yaml_lines(item, indent + 1)
                sub[0] = f'{pad}- {sub[0][len(pad) + 2:]}'
                lines.extend(sub)
            else:
                lines.append(f'{pad}- {yaml_scalar(item)}')
    return lines


def render_yaml(doc, header, comments):
    out = [f'# {line}'.rstrip() for line in header] + ['']
    for key, val in doc.items():
        if not isinstance(val, dict):
            out.append(f'{key}: {yaml_scalar(val)}')
            continue
        out += ['', f'{key}:']
        for i, (name, body) in enumerate(val.items()):
            if key == 'Resources' and i:
                out.append('')
            if name in comments:
                out.append(f'  # {comments[name]}')
            out += yaml_lines({name: body}, 1)
    return '\n'.join(out) + '\n'


def _as_plain(node):
    if isinstance(node, Ref):
        return {'Ref': node.name}
    if isinstance(node, dict):
        return {k: _as_plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_as_plain(v) for v in node]
    return node


def self_check(path, doc):
    """Read the written file back with PyYAML, when present, and compare it to the intent."""
    try:
        import yaml
    except ImportError:
        return 'skipped (PyYAML not installed)'

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor('!Ref', lambda loader, node: {'Ref': loader.construct_scalar(node)})
    with open(path, encoding='utf-8') as fh:
        parsed = yaml.load(fh, Loader=Loader)
    if parsed != _as_plain(doc):
        sys.exit(f'Self-check FAILED: {path} does not read back as the intended template. Do not deploy it.')
    return 'passed (the YAML reads back to exactly the live values)'


# ----------------------------------------------------------------------- records

def zone_fqdn(name):
    return name.strip().lower().rstrip('.') + '.'


def skip_reason(rr, fqdn):
    if rr['Name'].lower() == fqdn and rr['Type'] in ('NS', 'SOA'):
        return 'belongs to the hosted zone itself'
    if 'TrafficPolicyInstanceId' in rr:
        return 'owned by a traffic policy instance'
    if 'AliasTarget' not in rr and ('TTL' not in rr or not rr.get('ResourceRecords')):
        return 'has no alias target and no TTL/values'
    return None


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, '', [], {})}
    return value


def record_properties(rr):
    props = {'HostedZoneId': Ref('HostedZoneId'), 'Name': rr['Name'], 'Type': rr['Type']}
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
    """Readable, unique, alphanumeric ID, e.g. RecordWwwCNAME, RecordApexMX, RecordAppAWeighted."""
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


def describe(rr):
    text = f'{rr["Name"]} {rr["Type"]}'
    if '\\052' in rr['Name']:
        text += ' (\\052 is Route 53\'s escape for *)'
    if rr.get('SetIdentifier'):
        text += f' [{rr["SetIdentifier"]}]'
    if 'AliasTarget' in rr:
        text += f' -> alias {rr["AliasTarget"]["DNSName"]}'
    return text


def _resource(rtype, props):
    return {'Type': rtype, 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
            'Properties': props}


def records_document(zone, included):
    return {
        'AWSTemplateFormatVersion': '2010-09-09',
        'Description': f'Route 53 record sets of {zone["name"]}, adopted from the live hosted zone',
        'Parameters': {
            'HostedZoneId': {
                'Type': 'AWS::Route53::HostedZone::Id',
                'Description': f'ID of the existing public hosted zone {zone["name"]}',
            },
        },
        'Resources': {lid: _resource('AWS::Route53::RecordSet', props)
                      for lid, _, props, _ in included},
    }


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
        'Description': f'Public hosted zone {zone["name"]}, imported from the live zone',
        'Resources': {'HostedZone': _resource('AWS::Route53::HostedZone', props)},
    }


# --------------------------------------------------------------------------- AWS

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
        except Exception:  # pragma: no cover - informational only
            region = None
    return region or 'not set'


def write_text(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2, default=str) + '\n')


# -------------------------------------------------------------------------- main

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='Generate a CloudFormation template + parameters file from the live '
                    'records of a Route 53 public hosted zone.',
        epilog='See the top of this file for why the change set is CREATE rather than IMPORT.')
    parser.add_argument('--zone-name', help='public hosted zone name, e.g. tescoforbusiness.com')
    parser.add_argument('--zone-id', help='hosted zone ID (needed only if several zones share the name)')
    parser.add_argument('--stack-name', help='stack name used in the printed commands '
                                             '(default: <zone>-dns-records)')
    parser.add_argument('--prefix', help='file name prefix (default: zone name with dots as dashes)')
    parser.add_argument('--out-dir', default='.', help='where to write the files (default: here)')
    parser.add_argument('--only', metavar='REGEX', type=re.compile,
                        help='keep only records whose name matches REGEX (for a small trial stack)')
    parser.add_argument('--zone-import', action='store_true',
                        help='also write the files to IMPORT the hosted zone into its own stack')
    parser.add_argument('--from-file', metavar='JSON',
                        help='read records from a saved backup or list-resource-record-sets output')
    args = parser.parse_args(argv)
    if not (args.zone_name or args.zone_id or args.from_file):
        parser.error('give --zone-name or --zone-id')
    return args


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    captured_at = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    account, extras, backup = None, {}, None

    if args.from_file:
        zone, records = load_backup(args.from_file, args.zone_id, args.zone_name)
    else:
        if boto3 is None:
            sys.exit('boto3 is not installed (AWS CloudShell has it): pip3 install boto3')
        account = boto3.client('sts').get_caller_identity()['Account']
        r53 = boto3.client('route53')
        zone = resolve_zone(r53, args.zone_name, args.zone_id)
        records = fetch_records(r53, zone['id'])
        if args.zone_import:
            extras = fetch_zone_extras(r53, zone['id'])

    base = re.sub(r'[^a-z0-9-]', '-', zone['name'])
    if not base[:1].isalpha():
        base = 'zone-' + base
    suffix = '-subset' if args.only else ''
    prefix = args.prefix or base + suffix
    stack = args.stack_name or f'{base}-dns-records{suffix}'
    os.makedirs(args.out_dir, exist_ok=True)

    def out(name):
        return os.path.normpath(os.path.join(args.out_dir, name))

    # 1. Back up the zone before anything else.
    if not args.from_file:
        backup = out(f'{base}-backup-{now:%Y%m%dT%H%M%SZ}.json')
        write_json(backup, {'CapturedAt': captured_at, 'Account': account,
                            'HostedZoneId': zone['id'], 'HostedZoneName': zone['fqdn'],
                            'HostedZoneComment': zone['comment'], 'ResourceRecordSets': records})

    # 2. Decide what goes into the template.
    included, skipped, unselected, used = [], [], 0, set()
    for rr in records:
        reason = skip_reason(rr, zone['fqdn'])
        if reason:
            skipped.append((rr, reason))
        elif args.only and not args.only.search(rr['Name']):
            unselected += 1
        else:
            lid = logical_id(rr, zone['fqdn'], used)
            included.append((lid, rr, record_properties(rr), sorted(set(rr) - KNOWN_FIELDS)))
    if not included:
        sys.exit('No records left to put in the template.')

    # 3. Records template + parameters file.
    source = args.from_file or 'the live zone'
    header = [
        f'Route 53 record sets of {zone["fqdn"]} (hosted zone {zone["id"]}'
        + (f', account {account})' if account else ')'),
        f'Generated by r53_zone_to_cfn.py from {source} at {captured_at}.',
        '',
        'Values are copied verbatim from the Route 53 API. CloudFormation takes over an',
        'existing record only when the template matches it exactly, so deploy this file',
        'unchanged the first time, with a CREATE change set (record sets cannot be imported).',
        'DeletionPolicy/UpdateReplacePolicy Retain: deleting the stack or removing a',
        'resource from this template keeps the DNS record.',
    ]
    if skipped:
        header.append('Left out: ' + ', '.join(f'{rr["Name"]} {rr["Type"]}' for rr, _ in skipped))
    doc = records_document(zone, included)
    template = out(f'{prefix}-records.yml')
    params = out(f'{prefix}-records-params.json')
    write_text(template, render_yaml(doc, header, {lid: describe(rr) for lid, rr, _, _ in included}))
    write_json(params, [{'ParameterKey': 'HostedZoneId', 'ParameterValue': zone['id']}])
    check = self_check(template, doc)

    # 4. Optional: files to IMPORT the hosted zone itself.
    zone_template = zone_import = zone_check = None
    if args.zone_import:
        zone_header = [
            f'Public hosted zone {zone["fqdn"]} ({zone["id"]}), for an IMPORT change set.',
            f'Generated by r53_zone_to_cfn.py from {source} at {captured_at}.',
            'DeletionPolicy Retain: deleting this stack never deletes the hosted zone.',
        ]
        zdoc = zone_document(zone, extras)
        zone_template, zone_import = out(f'{base}-zone.yml'), out(f'{base}-zone-import.json')
        write_text(zone_template, render_yaml(zdoc, zone_header, {}))
        write_json(zone_import, [{'ResourceType': 'AWS::Route53::HostedZone',
                                  'LogicalResourceId': 'HostedZone',
                                  'ResourceIdentifier': {'Id': zone['id']}}])
        zone_check = self_check(zone_template, zdoc)

    # 5. Report.
    print(f'Hosted zone : {zone["fqdn"]}  {zone["id"]}')
    if account:
        print(f'Account     : {account}   (the stack will be created in {current_region()})')
    read = f'{len(records)} record sets'
    if zone['count'] is not None and zone['count'] != len(records):
        read += f' (the zone reports {zone["count"]})'
    print(f'Read        : {read}' + (f', backed up to {backup}' if backup else f' from {args.from_file}'))
    print(f'In template : {len(included)}')
    for rr, why in skipped:
        print(f'Left out    : {rr["Name"]} {rr["Type"]} - {why}')
    if unselected:
        print(f'Left out    : {unselected} record set(s) not matching --only')
    print()
    width = max(len(lid) for lid, *_ in included)
    for lid, rr, _, unknown in included:
        print(f'  {lid:<{width}}  {describe(rr)}')
        if unknown:
            print(f'  {"":<{width}}  WARNING: field(s) {", ".join(unknown)} not mapped; CloudFormation '
                  'may not match this record. Remove it from the template or handle it by hand.')
    size = os.path.getsize(template)
    print()
    print(f'Wrote       : {template} ({size:,} bytes), {params}')
    print(f'Self-check  : {check}')
    if size > TEMPLATE_BODY_LIMIT:
        print(f'NOTE        : over {TEMPLATE_BODY_LIMIT:,} bytes; upload the template to S3 and use '
              '--template-url instead of --template-body.')

    steps = [
        '',
        'Next steps (in this directory)',
        f'  1. Optional schema check: pip3 install --user cfn-lint && ~/.local/bin/cfn-lint {template}',
        f'  2. aws cloudformation validate-template --template-body file://{template}',
        '  3. aws cloudformation create-change-set \\',
        f'       --stack-name {stack} \\',
        '       --change-set-name adopt-existing-records \\',
        '       --change-set-type CREATE \\',
        f'       --template-body file://{template} \\',
        f'       --parameters file://{params} \\',
        f'       --description "Adopt the existing Route 53 records of {zone["name"]}"',
        f'  4. Console > CloudFormation > {stack} (REVIEW_IN_PROGRESS) > Change sets: every',
        '     record is listed as Add. Execute it; either stack-failure option is safe because',
        '     every record is Retain. If one record fails to match, the event names it: delete',
        '     the stack (all records stay), remove that record from the template, try again.',
    ]
    if backup:
        steps += [
            '  5. Confirm Route 53 itself did not change:',
            f'     diff <(jq -S .ResourceRecordSets {backup}) \\',
            f'          <(aws route53 list-resource-record-sets --hosted-zone-id {zone["id"]} '
            '| jq -S .ResourceRecordSets) && echo "zone unchanged"',
        ]
    if zone_template:
        steps += [
            '',
            f'Hosted zone import (a real IMPORT, into its own stack). Self-check: {zone_check}',
            '  aws cloudformation create-change-set \\',
            f'    --stack-name {base}-dns-zone \\',
            '    --change-set-name import-hosted-zone \\',
            '    --change-set-type IMPORT \\',
            f'    --template-body file://{zone_template} \\',
            f'    --resources-to-import file://{zone_import}',
            '  Review and execute it in the console the same way.',
        ]
        if args.from_file:
            steps.append('  NOTE: --from-file mode cannot read zone tags or query logging; add them by hand.')
    print('\n'.join(steps))


if __name__ == '__main__':
    main()
