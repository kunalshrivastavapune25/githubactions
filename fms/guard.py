#!/usr/bin/env python3
"""
fms_cfn_guard.py - safe CloudFormation updates for AWS Firewall Manager (FMS) policies.

CloudFormation updates AWS::FMS::Policy in place: every property of the type is
"Update requires: No interruption", so a change set that only edits a policy shows
Modify / Replacement False, and executing it calls FMS PutPolicy on the same policy
ID. What CloudFormation doesn't give you for FMS policies is drift detection, and
its change sets can't show what changed inside ManagedServiceData, which is one long
JSON string. This script adds both and stops the two things that delete policies.

  drift    Compare every FMS policy in the stack with the live policy, key by key, with
           ManagedServiceData parsed as JSON. Use it in place of CloudFormation drift
           detection, which leaves FMS policies NOT_CHECKED.
  plan     Check a change set before anyone executes it. Fails when an FMS policy would
           be deleted or replaced: a renamed logical ID shows up as Remove + Add, which
           deletes the policy and creates a new one. Prints a JSON-path diff for every
           modified policy and warns when the update would overwrite console changes.
  protect  One-time guard: a stack policy that denies Update:Replace and Update:Delete
           for AWS::FMS::Policy, so CloudFormation refuses to delete or replace a policy
           whatever the template says, plus stack termination protection.

Diff paths name list items where they can, for example
  ManagedServiceData.preProcessRuleGroups[AWSManagedRulesBotControlRuleSet]
      .ruleActionOverrides[TGT_VolumetricSession].actionToUse: {"challenge": {}} -> {"count": {}}

Run it where the stack lives (the FMS administrator account, the policies' region):
  python3 fms_cfn_guard.py drift   --stack-name fms-policies
  python3 fms_cfn_guard.py plan    --stack-name fms-policies --change-set-name my-change
  python3 fms_cfn_guard.py protect --stack-name fms-policies [--dry-run]
fms_deploy.py (diff, confirm, apply) imports this file, so keep the two side by side.

Exit codes: 0 OK; 1 blocked or error; 2 drift found (drift command).
Needs boto3 and, for YAML templates, PyYAML (pip3 install --user boto3 pyyaml).
"""

import argparse
import json
import re
import sys
import time

try:
    import boto3
except ImportError:
    boto3 = None

FMS_TYPE = 'AWS::FMS::Policy'
GONE = ('DELETE_COMPLETE', 'DELETE_SKIPPED')
EMPTY = (None, '', [])          # not {}: in WAF JSON {"count": {}} is a real action
TOKEN = '⟦{}⟧'        # ⟦...⟧ marks a value the template can't give
TOKEN_RE = re.compile('⟦[^⟧]*⟧')
TOKEN_SPLIT = re.compile('(⟦[^⟧]*⟧)')
BOOL_KEYS = {'RemediationEnabled', 'ExcludeResourceTags', 'ResourcesCleanUp', 'DeleteAllPolicyResources',
             'Egress', 'ForceRemediateForFirstEntries', 'ForceRemediateForLastEntries'}
NUMERIC_PARENTS = {'PortRange', 'IcmpTypeCode'}
SET_KEYS = ('ResourceTypeList', 'ResourceSetIds')
ALLOW_ALL = {'Effect': 'Allow', 'Action': 'Update:*', 'Principal': '*', 'Resource': '*'}
DENY_FMS = {'Effect': 'Deny', 'Action': ['Update:Replace', 'Update:Delete'], 'Principal': '*',
            'Resource': '*', 'Condition': {'StringEquals': {'ResourceType': [FMS_TYPE]}}}
NO_CHANGES = ("didn't contain changes", 'No updates are to be performed')


class _Marker:
    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return self.text


MISSING, NOVALUE, ORDER = _Marker('(absent)'), _Marker('AWS::NoValue'), _Marker('order')


# ------------------------------------------------------------------ templates

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


def load_template(body):
    """TemplateBody from get_template (a dict for JSON templates) or a template file's text."""
    if isinstance(body, dict):
        return json.loads(json.dumps(body))
    try:
        return json.loads(body)
    except ValueError:
        pass
    load = yaml_loader()
    if load is None:
        sys.exit('Reading a YAML template needs PyYAML: pip3 install --user pyyaml')
    return load(body) or {}


class Resolver:
    """Resolves Ref, Sub, Join, Select, Split, FindInMap and ImportValue the way the stack would.
    Anything else (GetAtt, If, ...) becomes a ⟦token⟧ that matches any live value."""

    def __init__(self, template, params, physical, aws, stack):
        self.params, self.physical = dict(params), physical
        self.declared = template.get('Parameters') or {}
        self.mappings = template.get('Mappings') or {}
        self.cfn, self._exports = aws.cfn, None
        self.pseudo = {'AWS::Region': aws.region, 'AWS::AccountId': aws.account,
                       'AWS::Partition': aws.partition, 'AWS::StackName': stack.get('StackName'),
                       'AWS::StackId': stack.get('StackId'),
                       'AWS::URLSuffix': 'amazonaws.com.cn' if aws.partition == 'aws-cn' else 'amazonaws.com'}

    def resolve(self, node):
        if isinstance(node, dict):
            if len(node) == 1:
                (fn, arg), = node.items()
                handler = {'Ref': self.ref, 'Fn::Sub': self.sub, 'Fn::Join': self.join,
                           'Fn::Select': self.select, 'Fn::Split': self.split,
                           'Fn::FindInMap': self.find_in_map, 'Fn::ImportValue': self.import_value}.get(fn)
                if handler:
                    return handler(arg)
                if fn == 'Fn::GetAtt':
                    return TOKEN.format('GetAtt ' + ('.'.join(map(str, arg)) if isinstance(arg, list) else str(arg)))
                if fn.startswith('Fn::') or fn == 'Condition':
                    return TOKEN.format(fn)
            out = {}
            for key, value in node.items():
                value = self.resolve(value)
                if value is not NOVALUE:
                    out[key] = value
            return out
        if isinstance(node, list):
            return [v for v in (self.resolve(x) for x in node) if v is not NOVALUE]
        return node

    def ref(self, name):
        if not isinstance(name, str):
            return TOKEN.format('Ref')
        if name == 'AWS::NoValue':
            return NOVALUE
        if self.pseudo.get(name):
            return self.pseudo[name]
        value = self.params.get(name)
        if value is None:
            value = (self.declared.get(name) or {}).get('Default')
        if value is not None:
            if 'List' in str((self.declared.get(name) or {}).get('Type', '')) and isinstance(value, str):
                return [v.strip() for v in value.split(',')]
            return value
        if self.physical.get(name):
            return self.physical[name]
        return TOKEN.format('Ref ' + name)

    def sub(self, arg):
        text, variables = (arg[0], arg[1] or {}) if isinstance(arg, list) and len(arg) == 2 else (arg, {})
        if not isinstance(text, str) or not isinstance(variables, dict):
            return TOKEN.format('Fn::Sub')
        variables = {k: self.resolve(v) for k, v in variables.items()}

        def replace(match):
            name = match.group(1)
            if name.startswith('!'):
                return '${' + name[1:] + '}'
            if name in variables:
                value = variables[name]
            elif '.' in name:
                value = TOKEN.format('GetAtt ' + name)
            else:
                value = self.ref(name)
            return value if isinstance(value, str) else TOKEN.format('Sub ' + name)

        return re.sub(r'\$\{([^}]*)\}', replace, text)

    def join(self, arg):
        if isinstance(arg, list) and len(arg) == 2:
            items = self.resolve(arg[1])
            if isinstance(arg[0], str) and isinstance(items, list) and all(isinstance(i, str) for i in items):
                return arg[0].join(items)
        return TOKEN.format('Fn::Join')

    def select(self, arg):
        if isinstance(arg, list) and len(arg) == 2:
            index, items = self.resolve(arg[0]), self.resolve(arg[1])
            try:
                return items[int(index)]
            except (TypeError, ValueError, IndexError, KeyError):
                pass
        return TOKEN.format('Fn::Select')

    def split(self, arg):
        if isinstance(arg, list) and len(arg) == 2:
            text = self.resolve(arg[1])
            if isinstance(arg[0], str) and isinstance(text, str):
                return text.split(arg[0])
        return TOKEN.format('Fn::Split')

    def find_in_map(self, arg):
        if isinstance(arg, list) and len(arg) == 3:
            keys = [self.resolve(a) for a in arg]
            try:
                return self.mappings[keys[0]][keys[1]][keys[2]]
            except (KeyError, TypeError):
                pass
        return TOKEN.format('Fn::FindInMap')

    def import_value(self, arg):
        name = self.resolve(arg)
        if isinstance(name, str):
            if self._exports is None:
                self._exports = {}
                for page in self.cfn.get_paginator('list_exports').paginate():
                    self._exports.update({e['Name']: e['Value'] for e in page.get('Exports', [])})
            if name in self._exports:
                return self._exports[name]
        return TOKEN.format('ImportValue ' + str(name))


# ---------------------------------------------------------- policy comparison

def live_as_cfn(policy, tags):
    """A GetPolicy result in the shape of AWS::FMS::Policy properties."""
    props = {k: policy[k] for k in ('PolicyName', 'PolicyDescription', 'RemediationEnabled',
                                    'ExcludeResourceTags', 'ResourceType', 'ResourceTypeList',
                                    'ResourceSetIds', 'ResourceTagLogicalOperator', 'ResourceTags')
             if k in policy}
    if 'DeleteUnusedFMManagedResources' in policy:
        props['ResourcesCleanUp'] = policy['DeleteUnusedFMManagedResources']
    for key in ('IncludeMap', 'ExcludeMap'):
        if policy.get(key):
            props[key] = {('ORGUNIT' if k == 'ORG_UNIT' else k): v for k, v in policy[key].items()}
    data = policy.get('SecurityServicePolicyData') or {}
    props['SecurityServicePolicyData'] = {k: data[k] for k in ('Type', 'ManagedServiceData', 'PolicyOption')
                                          if k in data}
    if tags is not None:  # aws:* tags (e.g. aws:cloudformation:stack-name) are AWS's, never the template's
        props['Tags'] = [{'Key': t['Key'], 'Value': t['Value']} for t in tags if not t['Key'].startswith('aws:')]
    return props


def _fix(node, parent=None):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            value = _fix(value, key)
            if key in BOOL_KEYS and isinstance(value, str) and value.lower() in ('true', 'false'):
                value = value.lower() == 'true'
            if parent in NUMERIC_PARENTS and isinstance(value, str) and re.fullmatch(r'-?\d+', value.strip()):
                value = int(value)
            out[key] = value
        return out
    if isinstance(node, list):
        return [_fix(v, parent) for v in node]
    return node


def _canon(value):
    return json.dumps(value, sort_keys=True, default=str)


def normalize(props):
    """Policy properties in a comparable form; ManagedServiceData becomes parsed JSON."""
    p = _fix(props if isinstance(props, dict) else {})
    p.pop('DeleteAllPolicyResources', None)  # write-only: FMS never returns it
    for key in SET_KEYS:
        if isinstance(p.get(key), list):
            p[key] = sorted(p[key], key=_canon)
    for key in ('IncludeMap', 'ExcludeMap'):
        if isinstance(p.get(key), dict):
            p[key] = {k: sorted(v, key=_canon) if isinstance(v, list) else v for k, v in p[key].items()}
    for key in ('ResourceTags', 'Tags'):
        if isinstance(p.get(key), list):
            p[key] = sorted(p[key], key=_canon)
    data = p.get('SecurityServicePolicyData')
    if isinstance(data, dict) and isinstance(data.get('ManagedServiceData'), str) and data['ManagedServiceData'].strip():
        try:
            p['SecurityServicePolicyData'] = dict(data, ManagedServiceData=json.loads(data['ManagedServiceData']))
        except ValueError:
            pass  # not plain JSON (for example a value the template can't give); compared as text
    return p


def _same(expected, actual):
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, str) and TOKEN_RE.search(expected):
        if TOKEN_RE.fullmatch(expected):
            return True  # the template's value isn't known here, so it can't differ
        pattern = ''.join('.*?' if TOKEN_RE.fullmatch(part) else re.escape(part)
                          for part in TOKEN_SPLIT.split(expected))
        return isinstance(actual, str) and re.fullmatch(pattern, actual, re.S) is not None
    return expected == actual


def _absent_like(value, lenient_false):
    return value in EMPTY or (lenient_false and value is False)


def _ident(item):
    """A readable name for a list item: a rule name, a managed rule group name, a rule group's name."""
    if not isinstance(item, dict):
        return None
    for key in ('name', 'Name', 'Key', 'actionName'):
        if isinstance(item.get(key), str) and item[key]:
            return item[key]
    group = item.get('managedRuleGroupIdentifier')
    if isinstance(group, dict) and isinstance(group.get('managedRuleGroupName'), str):
        return group['managedRuleGroupName']
    for key in ('ruleGroupArn', 'resourceARN'):
        arn = item.get(key)
        if isinstance(arn, str) and arn:
            parts = arn.split('/')
            return parts[-2] if len(parts) >= 3 and parts[-3].endswith('rulegroup') else parts[-1]
    for key in ('id', 'ruleGroupId', 'resourceId'):
        if isinstance(item.get(key), str) and item[key]:
            return item[key]
    return None


class DiffPath(str):
    """A diff path. Prints as a.b[name].c[0]; .parts keeps the steps for value_at()."""

    def __new__(cls, parts=()):
        text = ''
        for kind, value in parts:
            text += (f'.{value}' if text else str(value)) if kind == 'key' else f'[{value}]'
        path = super().__new__(cls, text)
        path.parts = tuple(parts)
        return path


def value_at(root, parts):
    """The value at a DiffPath's parts, or MISSING."""
    node = root
    for kind, value in parts:
        if kind == 'key':
            if not isinstance(node, dict) or value not in node:
                return MISSING
            node = node[value]
        elif kind == 'index':
            if not isinstance(node, list) or not 0 <= value < len(node):
                return MISSING
            node = node[value]
        else:
            hits = [x for x in node if _ident(x) == value] if isinstance(node, list) else []
            if len(hits) != 1:
                return MISSING
            node = hits[0]
    return node


def deep_diff(old, new, path='', lenient_false=False):
    """Yield (path, old, new) for every difference. Absent, null, "" and [] count as equal; with
    lenient_false, so does false (FMS fills in false defaults when it stores a policy)."""
    parts = path.parts if isinstance(path, DiffPath) else ((('key', path),) if path else ())
    yield from _diff(old, new, parts, lenient_false)


def _diff(old, new, parts, lenient_false):
    if isinstance(old, dict) and isinstance(new, dict):
        inner = []
        for key in list(old) + [k for k in new if k not in old]:
            sub = parts + (('key', key),)
            a, b = old.get(key, MISSING), new.get(key, MISSING)
            if b is MISSING and not _absent_like(a, lenient_false):
                inner.append((DiffPath(sub), a, MISSING))
            elif a is MISSING and not _absent_like(b, lenient_false):
                inner.append((DiffPath(sub), MISSING, b))
            elif a is not MISSING and b is not MISSING:
                inner.extend(_diff(a, b, sub, lenient_false))
        if inner and parts and old and new and not set(old) & set(new):
            yield DiffPath(parts), old, new  # one line for a switched object: {"challenge": {}} -> {"count": {}}
        else:
            yield from inner
    elif isinstance(old, list) and isinstance(new, list):
        yield from _list_diff(old, new, parts, lenient_false)
    elif not _same(old, new):
        yield DiffPath(parts), old, new


def _list_diff(old, new, parts, lenient_false):
    path = DiffPath(parts)
    if all(not isinstance(x, (dict, list)) for x in old + new):  # plain values: added / removed items
        left, added = list(old), []
        for item in new:
            hit = next((i for i, x in enumerate(left) if _same(x, item)), None)
            if hit is None:
                added.append(item)
            else:
                left.pop(hit)
        if not left and not added:
            if not all(_same(a, b) for a, b in zip(old, new)):
                yield path, ORDER, None
            return
        for item in left:
            yield path, item, MISSING
        for item in added:
            yield path, MISSING, item
        return
    ids_old, ids_new = [_ident(x) for x in old], [_ident(x) for x in new]
    if (all(ids_old) and all(ids_new) and len(set(ids_old)) == len(old) and len(set(ids_new)) == len(new)
            and not any(TOKEN_RE.search(i) for i in ids_old + ids_new)):
        by_new, seen = dict(zip(ids_new, new)), set(ids_old)
        for ident, item in zip(ids_old, old):
            sub = parts + (('item', ident),)
            if ident in by_new:
                yield from _diff(item, by_new[ident], sub, lenient_false)
            else:
                yield DiffPath(sub), item, MISSING
        for ident, item in zip(ids_new, new):
            if ident not in seen:
                yield DiffPath(parts + (('item', ident),)), MISSING, item
        kept_old = [i for i in ids_old if i in by_new]
        kept_new = [i for i in ids_new if i in seen]
        if kept_old != kept_new:
            yield path, ORDER, (kept_old, kept_new)
        return
    inner = [d for i, (a, b) in enumerate(zip(old, new)) for d in _diff(a, b, parts + (('index', i),), lenient_false)]
    if not inner and len(old) == len(new):
        return
    if len(old) == len(new) and sorted(map(_canon, old)) == sorted(map(_canon, new)):
        yield path, ORDER, None
        return
    yield from inner
    for i in range(len(new), len(old)):
        yield DiffPath(parts + (('index', i),)), old[i], MISSING
    for i in range(len(old), len(new)):
        yield DiffPath(parts + (('index', i),)), MISSING, new[i]


def show(value, limit=300):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + '...'


def render(diffs, indent='      '):
    lines = []
    for path, old, new in diffs:
        if old is ORDER:
            lines.append(f'{indent}~ {path}: order {show(new[0])} -> {show(new[1])}' if new
                         else f'{indent}~ {path}: same items, different order')
        elif new is MISSING:
            lines.append(f'{indent}- {path}: {show(old)}')
        elif old is MISSING:
            lines.append(f'{indent}+ {path}: {show(new)}')
        else:
            lines.append(f'{indent}~ {path}: {show(old)} -> {show(new)}')
    return '\n'.join(lines)


def policy_diff(r_old, old_resource, r_new, new_resource):
    """Differences between two template definitions of one FMS policy."""
    old_p = normalize(r_old.resolve((old_resource or {}).get('Properties') or {}))
    new_p = normalize(r_new.resolve((new_resource or {}).get('Properties') or {}))
    return list(deep_diff(old_p, new_p))


def policy_name(resolver, resource):
    name = resolver.resolve(((resource or {}).get('Properties') or {}).get('PolicyName'))
    return name if isinstance(name, str) else '?'


# --------------------------------------------------------------------- AWS

class Aws:
    def __init__(self, region=None):
        if boto3 is None:
            sys.exit('boto3 is not installed: pip3 install --user boto3')
        session = boto3.session.Session(region_name=region)
        if not session.region_name:
            sys.exit('No AWS region set: pass --region or export AWS_REGION.')
        try:
            from botocore.config import Config
            config = Config(retries={'max_attempts': 10, 'mode': 'standard'})
        except ImportError:
            config = None
        self.region = session.region_name
        self.cfn = session.client('cloudformation', config=config)
        self.fms = session.client('fms', config=config)
        self.s3 = session.client('s3', config=config)
        identity = session.client('sts').get_caller_identity()
        self.account, self.partition = identity['Account'], identity['Arn'].split(':')[1]


def stack_info(cfn, name):
    stack = cfn.describe_stacks(StackName=name)['Stacks'][0]
    params = {p['ParameterKey']: p.get('ResolvedValue', p.get('ParameterValue'))
              for p in stack.get('Parameters', [])}
    return stack, params


def physical_ids(cfn, name):
    ids = {}
    for page in cfn.get_paginator('list_stack_resources').paginate(StackName=name):
        for res in page['StackResourceSummaries']:
            if res.get('ResourceStatus') not in GONE and res.get('PhysicalResourceId'):
                ids[res['LogicalResourceId']] = res['PhysicalResourceId']
    return ids


def deployed_template(cfn, name):
    return cfn.get_template(StackName=name, TemplateStage='Original')['TemplateBody']


def describe_change_set(cfn, stack, name, wait):
    deadline = time.time() + wait
    while True:
        first, token, changes = None, None, []
        while True:
            kwargs = {'StackName': stack, 'ChangeSetName': name}
            if token:
                kwargs['NextToken'] = token
            page = cfn.describe_change_set(**kwargs)
            first = first or page
            changes += page.get('Changes', [])
            token = page.get('NextToken')
            if not token:
                break
        if first['Status'] not in ('CREATE_PENDING', 'CREATE_IN_PROGRESS'):
            first['Changes'] = changes
            return first
        if time.time() > deadline:
            sys.exit(f'Change set {name} is still {first["Status"]} after {wait}s.')
        time.sleep(5)


def live_policy(aws, policy_id):
    """(CloudFormation-shaped properties, policy name) of the live policy, or None if it's gone."""
    try:
        live = aws.fms.get_policy(PolicyId=policy_id)
    except Exception as exc:
        if 'ResourceNotFound' in f'{type(exc).__name__} {exc}':
            return None
        raise
    tags = aws.fms.list_tags_for_resource(ResourceArn=live['PolicyArn']).get('TagList', [])
    return live_as_cfn(live['Policy'], tags), live['Policy'].get('PolicyName')


def drift_of(aws, resolver, props, policy_id):
    """None when the policy no longer exists, else the differences template -> live
    (only for properties the template sets, as CloudFormation drift detection does)."""
    found = live_policy(aws, policy_id)
    if found is None:
        return None
    expected = normalize(resolver.resolve(props))
    actual = normalize(found[0])
    actual = {k: v for k, v in actual.items() if k in expected}
    return list(deep_diff(expected, actual, '', lenient_false=True))


# ------------------------------------------------------------ change set review

class Review:
    """What a change set does to the stack's FMS policies, and whether it is safe to execute."""

    def __init__(self, stack_name, change_set_name, change_set):
        self.stack_name, self.change_set_name = stack_name, change_set_name
        self.status = change_set['Status']
        self.reason = change_set.get('StatusReason') or ''
        self.no_changes = self.status == 'FAILED' and any(text in self.reason for text in NO_CHANGES)
        self.failed = self.status == 'FAILED' and not self.no_changes
        self.changes = [c['ResourceChange'] for c in change_set.get('Changes', []) if c.get('Type') == 'Resource']
        self.parameters = change_set.get('Parameters', [])
        self.details, self.notes, self.warnings, self.blocked = [], [], [], []
        self.modified, self.created = [], []

    def exit_code(self, strict=False):
        if self.failed or self.blocked or (strict and self.warnings):
            return 1
        return 0

    def report(self, strict=False):
        if self.no_changes:
            print(f'Change set {self.change_set_name}: no changes, nothing to execute.')
            return
        if self.failed:
            print(f'Change set {self.change_set_name} FAILED: {self.reason}')
            return
        print(f'Change set {self.change_set_name} on {self.stack_name} ({self.status}): '
              f'{len(self.changes)} resource change(s)')
        for c in self.changes:
            rep = f'  Replacement: {c["Replacement"]}' if c.get('Replacement') else ''
            print(f'  {c["Action"]:<8}{c["LogicalResourceId"]:<44}{c["ResourceType"]}{rep}')
        for line in self.details:
            print(line)
        tagged = [f'{label} {item}' for label, items in (('NOTE    ', self.notes), ('WARNING ', self.warnings),
                                                        ('BLOCKED ', self.blocked)) for item in items]
        if tagged:
            print('\n' + '\n'.join(tagged))
        print()
        if self.blocked:
            print('Do not execute this change set.')
        elif strict and self.warnings:
            print('Stopping because of the warnings (--strict).')
        elif any(c['ResourceType'] == FMS_TYPE for c in self.changes):
            print(f'OK: {len(self.modified)} FMS policy update(s) in place, {len(self.created)} new; '
                  'none deleted or replaced.')
        else:
            print('OK: no FMS policy changes in this change set.')


def review_change_set(aws, stack_name, change_set_name, wait=300, check_live=True):
    """check_live=False skips the live-policy comparison (when the caller already showed it)."""
    review = Review(stack_name, change_set_name, describe_change_set(aws.cfn, stack_name, change_set_name, wait))
    if review.no_changes or review.failed:
        return review
    stack, old_params = stack_info(aws.cfn, stack_name)
    is_new = stack['StackStatus'] == 'REVIEW_IN_PROGRESS'
    old_t = {} if is_new else load_template(deployed_template(aws.cfn, stack_name))
    new_t = load_template(aws.cfn.get_template(StackName=stack_name, ChangeSetName=change_set_name,
                                               TemplateStage='Original')['TemplateBody'])
    ids = {} if is_new else physical_ids(aws.cfn, stack_name)
    new_params = {p['ParameterKey']: p.get('ResolvedValue', p.get('ParameterValue'))
                  for p in review.parameters if p.get('ParameterValue') is not None or p.get('ResolvedValue')}
    r_old = Resolver(old_t, old_params, ids, aws, stack)
    r_new = Resolver(new_t, dict(old_params, **new_params), ids, aws, stack)
    old_res, new_res = old_t.get('Resources') or {}, new_t.get('Resources') or {}

    fms = [c for c in review.changes if c['ResourceType'] == FMS_TYPE]
    removed = {c['LogicalResourceId']: c for c in fms if c['Action'] == 'Remove'}
    removed_names = {lid: policy_name(r_old, old_res.get(lid)) for lid in removed}
    added_names = {c['LogicalResourceId']: policy_name(r_new, new_res.get(c['LogicalResourceId']))
                   for c in fms if c['Action'] == 'Add'}

    for lid, name in removed_names.items():
        keeps = ' (DeletionPolicy Retain keeps it, but it leaves the stack)' \
            if removed[lid].get('PolicyAction') == 'Retain' else ''
        twins = [n for n, other in added_names.items() if other == name]
        if twins:
            review.blocked.append(f'{lid} ("{name}") would be removed{keeps} and {twins[0]} created as a new '
                                  f'policy with the same name: the logical ID was renamed. Use {lid} as the '
                                  'logical ID in the template.')
        else:
            review.blocked.append(f'{lid} ("{name}", {ids.get(lid, "?")}) would be deleted{keeps}: it is '
                                  'missing from the new template. Put it back.')
    live_names = None
    for lid, name in added_names.items():
        if name in removed_names.values():
            continue
        if live_names is None:
            live_names = {}
            for page in aws.fms.get_paginator('list_policies').paginate():
                live_names.update({p['PolicyName']: p['PolicyId'] for p in page.get('PolicyList', [])})
        if name in live_names:
            review.blocked.append(f'{lid} would create a second policy named "{name}"; one already exists '
                                  f'({live_names[name]}) and is not in this stack. Import it instead.')
        else:
            review.created.append(lid)
            review.notes.append(f'{lid} creates a new FMS policy "{name}".')

    for c in fms:
        lid = c['LogicalResourceId']
        replaced = c['Action'] == 'Modify' and c.get('Replacement') in ('True', 'Conditional')
        if replaced:
            review.blocked.append(f'{lid} would be REPLACED (Replacement: {c["Replacement"]}): a new policy '
                                  'would be created and this one deleted.')
        if c['Action'] != 'Modify' or lid not in old_res or lid not in new_res:
            continue
        if not replaced:
            review.modified.append(lid)
        name = policy_name(r_new, new_res[lid])
        diffs = policy_diff(r_old, old_res[lid], r_new, new_res[lid])
        how = 'would be REPLACED' if replaced else 'updated in place'
        review.details.append(f'\n{lid} ("{name}", {ids.get(lid, "?")}): {how}, deployed template -> new template')
        if diffs:
            review.details.append(render(diffs))
        else:
            review.details.append('      no difference after parsing ManagedServiceData')
            if not replaced:
                review.warnings.append(f'{lid} ("{name}") only differs in formatting, yet CloudFormation will '
                                       'still send it to FMS (unchanged in meaning). To leave it untouched, use '
                                       'its text from the deployed template.')
        if lid in ids and check_live:
            drift = drift_of(aws, r_old, old_res[lid].get('Properties') or {}, ids[lid])
            if drift is None:
                review.blocked.append(f'{lid}: policy {ids[lid]} no longer exists in Firewall Manager, so the '
                                      'update would fail. Re-import or remove it from the stack first.')
            elif drift:
                review.warnings.append(f'{lid} ("{name}") was changed outside CloudFormation; executing this '
                                       'change set overwrites those changes with the template values.')
                review.details.append('    live policy vs deployed template (console changes this update would '
                                      'overwrite):')
                review.details.append(render(drift, '        '))

    for lid, res in new_res.items():
        if isinstance(res, dict) and res.get('Type') == FMS_TYPE:
            if res.get('DeletionPolicy') not in ('Retain', 'RetainExceptOnCreate'):
                review.warnings.append(f'{lid} has no DeletionPolicy Retain: deleting the stack would delete '
                                       'the policy.')
            if str((res.get('Properties') or {}).get('DeleteAllPolicyResources', '')).lower() == 'true':
                review.warnings.append(f'{lid} sets DeleteAllPolicyResources: true; if CloudFormation ever '
                                       'deletes it, FMS also deletes the web ACLs or security groups it created.')
    return review


# ---------------------------------------------------------------- commands

def cmd_drift(args, aws):
    stack, params = stack_info(aws.cfn, args.stack_name)
    template = load_template(deployed_template(aws.cfn, args.stack_name))
    ids = physical_ids(aws.cfn, args.stack_name)
    resolver = Resolver(template, params, ids, aws, stack)
    policies = [(lid, res) for lid, res in (template.get('Resources') or {}).items()
                if isinstance(res, dict) and res.get('Type') == FMS_TYPE and lid in ids]
    print(f'Stack {args.stack_name}: {len(policies)} FMS polic{"y" if len(policies) == 1 else "ies"} '
          'compared with Firewall Manager (template -> live)')
    drifted = 0
    for lid, res in policies:
        name = policy_name(resolver, res)
        diffs = drift_of(aws, resolver, res.get('Properties') or {}, ids[lid])
        if diffs is None:
            drifted += 1
            print(f'  DELETED  {lid} ("{name}", {ids[lid]}): no longer exists in Firewall Manager')
        elif diffs:
            drifted += 1
            print(f'  DRIFTED  {lid} ("{name}", {ids[lid]})')
            print(render(diffs))
        else:
            print(f'  IN_SYNC  {lid} ("{name}", {ids[lid]})')
    if drifted:
        print(f'\n{drifted} polic{"y has" if drifted == 1 else "ies have"} changed outside CloudFormation. '
              'Put the live values into the template (or run the pipeline to restore the template '
              'values) before the next update.')
        return 2
    print('\nAll FMS policies match the template.')
    return 0


def cmd_plan(args, aws):
    review = review_change_set(aws, args.stack_name, args.change_set_name, args.wait)
    review.report(args.strict)
    return review.exit_code(args.strict)


def cmd_protect(args, aws):
    body = aws.cfn.get_stack_policy(StackName=args.stack_name).get('StackPolicyBody')
    if body:
        policy = json.loads(body)
        statements = policy.setdefault('Statement', [])
        changed = not any(_canon(s) == _canon(DENY_FMS) for s in statements)
        if changed:
            statements.append(DENY_FMS)
    else:
        policy, changed = {'Statement': [ALLOW_ALL, DENY_FMS]}, True
    stack, _ = stack_info(aws.cfn, args.stack_name)
    protected = stack.get('EnableTerminationProtection', False)
    print(json.dumps(policy, indent=2))
    if args.dry_run:
        print('\nDry run: nothing changed.' + ('' if protected or args.skip_termination_protection
                                                else ' Termination protection would be enabled.'))
        return 0
    if changed:
        aws.cfn.set_stack_policy(StackName=args.stack_name, StackPolicyBody=json.dumps(policy))
        print(f'\nStack policy set on {args.stack_name}: FMS policies can be updated in place, '
              'never replaced or deleted by a stack update.')
    else:
        print(f'\n{args.stack_name} already has this protection.')
    if not protected and not args.skip_termination_protection:
        aws.cfn.update_termination_protection(EnableTerminationProtection=True, StackName=args.stack_name)
        print('Termination protection enabled.')
    return 0


def main(argv=None, aws_factory=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--stack-name', required=True)
    common.add_argument('--region', help='defaults to AWS_REGION / your CLI profile')
    parser = argparse.ArgumentParser(description='Safe CloudFormation updates for AWS Firewall Manager policies.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('drift', parents=[common], help='compare FMS policies in the stack with the live policies')
    plan = sub.add_parser('plan', parents=[common], help='check a change set before it is executed')
    plan.add_argument('--change-set-name', required=True)
    plan.add_argument('--strict', action='store_true', help='also fail on warnings, e.g. console changes '
                                                            'that the update would overwrite')
    plan.add_argument('--wait', type=int, default=300, help='seconds to wait for the change set (default 300)')
    protect = sub.add_parser('protect', parents=[common], help='stack policy + termination protection')
    protect.add_argument('--dry-run', action='store_true')
    protect.add_argument('--skip-termination-protection', action='store_true')
    args = parser.parse_args(argv)
    aws = (aws_factory or Aws)(args.region)
    return {'drift': cmd_drift, 'plan': cmd_plan, 'protect': cmd_protect}[args.command](args, aws)


if __name__ == '__main__':
    sys.exit(main())
