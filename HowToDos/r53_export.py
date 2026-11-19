#!/usr/bin/env python3
"""
r53_export.py - export ONE Route 53 hosted zone and its records to CloudFormation
YAML, leaving out whatever CloudFormation already manages.

    python3 r53_export.py example.com --region eu-west-1 [--profile myprofile]

1. Finds the hosted zone (name or zone ID) and checks every enabled region for a
   CloudFormation stack that already holds it (or only --regions a,b).
2. No stack holds the zone: writes <zone>-hostedzone.yaml and <zone>-import.json
   and prints the two commands to import it. Run the script again afterwards to
   export the records.
3. A stack holds the zone: writes <zone>-records.yaml with every record no stack
   manages yet, to add under Resources: in that stack's template. Left out: the
   zone's own NS and SOA records (at the zone apex), records already in any stack,
   and records created by a traffic policy.
     --full   writes <zone>-stack.yaml instead: the stack's current template plus
              those records, ready to deploy as an update.
     --max N  exports only the first N new records. Deploy them, run again for
              the next N (useful for big zones).

Records are copied exactly as Route 53 returns them and carry DeletionPolicy and
UpdateReplacePolicy Retain. Keep both: CloudFormation only takes over an existing
record when the template matches it exactly, and Retain stops a failed update's
rollback from deleting live records.

Requires Python 3.8+, boto3 and PyYAML:  pip install boto3 pyyaml
"""

import argparse
import collections
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

RECORDSET = "AWS::Route53::RecordSet"
RECORDSETGROUP = "AWS::Route53::RecordSetGroup"
HOSTEDZONE = "AWS::Route53::HostedZone"
BODY_LIMIT = 51200
STACK_RESOURCE_LIMIT = 500
KNOWN_FIELDS = {"Name", "Type", "SetIdentifier", "Weight", "Region", "GeoLocation",
                "Failover", "MultiValueAnswer", "TTL", "ResourceRecords", "AliasTarget",
                "HealthCheckId", "TrafficPolicyInstanceId", "CidrRoutingConfig",
                "GeoProximityLocation"}


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------------------ names

_OCTAL = re.compile(r"\\([0-7]{3})")


def decode_name(name):
    """Route 53 returns special characters as \\ooo octal; * comes back as \\052."""
    def repl(m):
        ch = chr(int(m.group(1), 8))
        return ch if ch.isprintable() and ch not in ".\\" else m.group(0)
    return _OCTAL.sub(repl, name)


def norm_name(name):
    """For comparing names only: decoded, lower case, no trailing dot."""
    return decode_name(name).lower().rstrip(".")


def labels_under(name, zone_name):
    n, z = norm_name(name), norm_name(zone_name)
    if n == z:
        return []
    if n.endswith("." + z):
        n = n[: -len(z) - 1]
    return n.split(".")


def _camel(text):
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", text or "") if p]
    return "".join(p[0].upper() + p[1:].lower() for p in parts)


def base_logical_id(name, zone_name, rtype, set_id):
    labels = labels_under(name, zone_name)
    rel = "".join("Wildcard" if l == "*" else _camel(l) for l in labels) if labels else "Apex"
    lid = "Record" + rel + _camel(rtype) + _camel(set_id)
    if len(lid) > 200:
        lid = lid[:200] + "X" + hashlib.sha1(lid.encode()).hexdigest()[:8].upper()
    return lid


# --------------------------------------------------------- records -> template

def skip_reason(rr, zone_name):
    apex = norm_name(rr["Name"]) == norm_name(zone_name)
    if apex and rr["Type"] in ("SOA", "NS"):
        return f"the zone's own {rr['Type']} record"
    if rr.get("TrafficPolicyInstanceId"):
        return "created by a Route 53 traffic policy"
    return None


def record_warnings(rr):
    notes = []
    values = [r.get("Value", "") for r in rr.get("ResourceRecords") or []]
    if rr["Type"] == "TXT" and any("heritage=external-dns" in v for v in values):
        notes.append("looks like an ExternalDNS ownership record; ExternalDNS may keep changing this name")
    unknown = sorted(set(rr) - KNOWN_FIELDS)
    if unknown:
        notes.append("has settings this script does not copy: " + ", ".join(unknown))
    return notes


def rrset_to_props(rr, zone_id):
    """Copy the record exactly as Route 53 returned it; nothing is normalised."""
    p = {"HostedZoneId": zone_id, "Name": rr["Name"], "Type": rr["Type"]}
    for k in ("SetIdentifier", "Weight", "Region"):
        if rr.get(k) is not None:
            p[k] = rr[k]
    if rr.get("GeoLocation"):
        p["GeoLocation"] = {k: rr["GeoLocation"][k]
                            for k in ("ContinentCode", "CountryCode", "SubdivisionCode")
                            if k in rr["GeoLocation"]}
    if rr.get("GeoProximityLocation"):
        src, gp = rr["GeoProximityLocation"], {}
        for k in ("AWSRegion", "LocalZoneGroup"):
            if k in src:
                gp[k] = src[k]
        if "Coordinates" in src:
            gp["Coordinates"] = {k: src["Coordinates"][k]
                                 for k in ("Latitude", "Longitude") if k in src["Coordinates"]}
        if "Bias" in src:
            gp["Bias"] = src["Bias"]
        p["GeoProximityLocation"] = gp
    if rr.get("CidrRoutingConfig"):
        p["CidrRoutingConfig"] = {k: rr["CidrRoutingConfig"][k]
                                  for k in ("CollectionId", "LocationName")
                                  if k in rr["CidrRoutingConfig"]}
    for k in ("Failover", "MultiValueAnswer", "HealthCheckId"):
        if rr.get(k) is not None:
            p[k] = rr[k]
    if rr.get("TTL") is not None:
        p["TTL"] = str(rr["TTL"])
    if rr.get("ResourceRecords"):
        p["ResourceRecords"] = [r["Value"] for r in rr["ResourceRecords"]]
    if rr.get("AliasTarget"):
        at = rr["AliasTarget"]
        p["AliasTarget"] = {"HostedZoneId": at["HostedZoneId"], "DNSName": at["DNSName"],
                            "EvaluateTargetHealth": bool(at.get("EvaluateTargetHealth", False))}
    return p


def key_from_props(props):
    name, rtype, sid = props.get("Name"), props.get("Type"), props.get("SetIdentifier", "")
    if isinstance(name, str) and isinstance(rtype, str) and isinstance(sid, str):
        return (norm_name(name), rtype.upper(), sid)
    return None


def plan_records(zone, rrsets, template, own_stack, record_owners):
    """Split the zone's records into new / already managed / skipped.

    template       the zone stack's current template (dict)
    own_stack      (region, stack name) of the zone's stack
    record_owners  {norm_name: [(region, stack, logical_id), ...]} from every stack
    Returns (new [(logical_id, resource)], already [(label, logical_id)],
             skipped [(label, reason)], warnings [text]).
    """
    resources = template.get("Resources") or {}
    managed, literal_ids = {}, set()
    for lid, res in resources.items():
        res = res or {}
        props = res.get("Properties") or {}
        if res.get("Type") == RECORDSET:
            k = key_from_props(props)
            if k:
                managed[k] = lid
                literal_ids.add(lid)
        elif res.get("Type") == RECORDSETGROUP:
            for rs in props.get("RecordSets") or []:
                k = key_from_props(rs) if isinstance(rs, dict) else None
                if k:
                    managed[k] = lid

    already, skipped, warnings, keep = [], [], [], []
    for rr in rrsets:
        sid = rr.get("SetIdentifier", "")
        key = (norm_name(rr["Name"]), rr["Type"], sid)
        label = f"{rr['Name']} {rr['Type']}" + (f" [{sid}]" if sid else "")
        reason = skip_reason(rr, zone["Name"])
        if reason:
            skipped.append((label, reason))
            continue
        if key in managed:
            already.append((label, managed[key]))
            keep.append((key, rr, None))
            continue
        # Owned by another stack, or by a record in this stack whose name is computed.
        others = [o for o in record_owners.get(key[0], [])
                  if (o[0], o[1]) != own_stack or o[2] not in literal_ids]
        if others:
            owners = sorted({f"stack {o[1]} ({o[0]})" for o in others})
            skipped.append((label, "already managed by " + ", ".join(owners)))
            continue
        warnings += [f"{label}: {w}" for w in record_warnings(rr)]
        keep.append((key, rr, rrset_to_props(rr, zone["Id"])))

    # Collisions are judged across every kept record, so a record keeps the same
    # logical ID whether it is exported now or on a later run.
    bases = {key: base_logical_id(rr["Name"], zone["Name"], rr["Type"], key[2]) for key, rr, _ in keep}
    counts = collections.Counter(bases.values())
    used = set(resources)
    todo = [t for t in keep if t[2] is not None]
    todo.sort(key=lambda t: (list(reversed(labels_under(t[1]["Name"], zone["Name"]))), t[1]["Type"], t[0][2]))
    new = []
    for key, rr, props in todo:
        lid = bases[key]
        if counts[lid] > 1 or lid in used:
            lid = lid + "X" + hashlib.sha1("|".join(key).encode()).hexdigest()[:8].upper()
        while lid in used:
            lid += "X"
        used.add(lid)
        new.append((lid, {"Type": RECORDSET, "DeletionPolicy": "Retain",
                          "UpdateReplacePolicy": "Retain", "Properties": props}))
    return new, already, skipped, warnings


# ------------------------------------------------------------------------- YAML

_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_./:-]*$")
_RESERVED = {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}


def yaml_scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if _PLAIN.match(s) and not s.endswith(":") and s.lower() not in _RESERVED:
        return s
    if all(32 <= ord(c) < 127 for c in s):
        return "'" + s.replace("'", "''") + "'"
    return json.dumps(s)  # a JSON string is a valid YAML double-quoted scalar


def _emit_map(d, indent, out):
    pad = " " * indent
    for k, v in d.items():
        key = yaml_scalar(str(k))
        if not isinstance(v, (dict, list)):
            out.append(f"{pad}{key}: {yaml_scalar(v)}")
        elif not v:
            out.append(f"{pad}{key}: {'{}' if isinstance(v, dict) else '[]'}")
        else:
            out.append(f"{pad}{key}:")
            (_emit_map if isinstance(v, dict) else _emit_seq)(v, indent + 2, out)


def _emit_seq(seq, indent, out):
    pad = " " * indent
    for item in seq:
        if not isinstance(item, (dict, list)):
            out.append(f"{pad}- {yaml_scalar(item)}")
        elif not item:
            out.append(f"{pad}- {'{}' if isinstance(item, dict) else '[]'}")
        else:
            sub = []
            (_emit_map if isinstance(item, dict) else _emit_seq)(item, indent + 2, sub)
            sub[0] = pad + "- " + sub[0][indent + 2:]
            out.extend(sub)


def to_yaml(obj, header=""):
    out = [f"# {line}".rstrip() for line in header.splitlines()] if header else []
    _emit_map(obj, 0, out)
    return "\n".join(out) + "\n"


def load_template(body):
    """Parse GetTemplate's body (a dict for JSON, text for YAML). Short-form tags
    (!Ref, !Sub, ...) become their long form, which CloudFormation treats the same."""
    if isinstance(body, dict):
        return body
    if body.lstrip().startswith("{"):
        return json.loads(body)
    try:
        import yaml
    except ImportError:
        die("This stack's template is YAML: pip install pyyaml")

    class CfnLoader(yaml.SafeLoader):
        pass

    # Keep values such as 2010-09-09 as text, not dates.
    CfnLoader.yaml_implicit_resolvers = {
        ch: [r for r in rs if r[0] != "tag:yaml.org,2002:timestamp"]
        for ch, rs in yaml.SafeLoader.yaml_implicit_resolvers.items()}

    def construct(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        if suffix in ("Ref", "Condition"):
            return {suffix: value}
        if suffix == "GetAtt" and isinstance(value, str):
            value = value.split(".", 1)
        return {"Fn::" + suffix: value}

    CfnLoader.add_multi_constructor("!", construct)
    return yaml.load(body, Loader=CfnLoader)


# -------------------------------------------------------------------------- AWS

class Aws:
    def __init__(self, profile=None, region=None):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            die("pip install boto3 pyyaml")
        self.session = boto3.Session(profile_name=profile, region_name=region)
        self.region = region or self.session.region_name
        if not self.region:
            die("Pass --region (the region your zone stacks are in)")
        self._cfg = Config(retries={"max_attempts": 10, "mode": "adaptive"})
        self._clients = {}

    def client(self, service, region=None):
        key = (service, region or self.region)
        if key not in self._clients:
            self._clients[key] = self.session.client(service, region_name=key[1], config=self._cfg)
        return self._clients[key]

    @property
    def r53(self):
        return self.client("route53", "us-east-1")

    def cfn(self, region=None):
        return self.client("cloudformation", region)


def _err_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)


def find_zone(r53, ref):
    zones = []
    for page in r53.get_paginator("list_hosted_zones").paginate():
        for z in page["HostedZones"]:
            zones.append({"Id": z["Id"].split("/")[-1], "Name": z["Name"],
                          "Private": bool(z.get("Config", {}).get("PrivateZone")),
                          "LinkedService": z.get("LinkedService")})
    ref_id = ref.split("/")[-1]
    for z in zones:
        if z["Id"] == ref_id:
            return z
    matches = [z for z in zones if norm_name(z["Name"]) == norm_name(ref)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        die(f"No hosted zone named or with ID {ref} in this account")
    ids = ", ".join(f"{z['Id']} ({'private' if z['Private'] else 'public'})" for z in matches)
    die(f"{len(matches)} zones are named {ref}: {ids}. Run again with the zone ID.")


def list_records(r53, zone_id):
    return [rr for page in r53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone_id)
            for rr in page["ResourceRecordSets"]]


def regions_to_scan(aws, spec):
    if spec == "all":
        try:
            regions = [r["RegionName"] for r in aws.client("ec2").describe_regions()["Regions"]]
        except Exception as e:  # noqa: BLE001
            print(f"  could not list regions ({_err_code(e)}); checking {aws.region} only")
            regions = [aws.region]
    else:
        regions = [r.strip() for r in (spec or "").split(",") if r.strip()]
    if aws.region not in regions:
        regions.insert(0, aws.region)
    return regions


def scan_stacks(aws, regions):
    """Which stack holds each hosted zone, and which records any stack manages."""
    zone_owner = collections.defaultdict(list)     # zone_id -> [(region, stack, logical_id)]
    record_owners = collections.defaultdict(list)  # norm_name -> [(region, stack, logical_id)]
    notes, blocked = [], []
    print(f"Checking CloudFormation stacks in {len(regions)} region(s)...")
    for region in regions:
        cfn = aws.cfn(region)
        try:
            stacks = [s for page in cfn.get_paginator("list_stacks").paginate()
                      for s in page["StackSummaries"] if s["StackStatus"] != "DELETE_COMPLETE"]
        except Exception as e:  # noqa: BLE001
            blocked.append(f"{region} ({_err_code(e)})")
            continue
        for s in stacks:
            name = s["StackName"]
            try:
                resources = [r for page in cfn.get_paginator("list_stack_resources").paginate(StackName=s["StackId"])
                             for r in page["StackResourceSummaries"]]
            except Exception as e:  # noqa: BLE001
                notes.append(f"{region}/{name}: could not list resources ({_err_code(e)})")
                continue
            has_group = False
            for r in resources:
                if r.get("ResourceStatus") == "DELETE_COMPLETE":
                    continue
                rtype, pid, lid = r["ResourceType"], r.get("PhysicalResourceId") or "", r["LogicalResourceId"]
                if rtype == HOSTEDZONE and pid:
                    zone_owner[pid.split("/")[-1]].append((region, name, lid))
                elif rtype == RECORDSET and pid:
                    record_owners[norm_name(pid)].append((region, name, lid))
                elif rtype == RECORDSETGROUP:
                    has_group = True
            if has_group:
                try:
                    tpl = load_template(cfn.get_template(StackName=s["StackId"],
                                                         TemplateStage="Original")["TemplateBody"])
                except Exception as e:  # noqa: BLE001
                    notes.append(f"{region}/{name}: has a RecordSetGroup but its template is unreadable "
                                 f"({_err_code(e)}); check it by hand")
                    continue
                for lid, res in (tpl.get("Resources") or {}).items():
                    if (res or {}).get("Type") != RECORDSETGROUP:
                        continue
                    for rs in (res.get("Properties") or {}).get("RecordSets") or []:
                        n = rs.get("Name") if isinstance(rs, dict) else None
                        if isinstance(n, str):
                            record_owners[norm_name(n)].append((region, name, lid))
                        else:
                            notes.append(f"{region}/{name}/{lid}: RecordSetGroup with a computed name; check it by hand")
    if blocked:
        notes.insert(0, f"could not check {len(blocked)} region(s), usually blocked by an SCP: " + ", ".join(blocked))
    return zone_owner, record_owners, notes


def zone_properties(r53, zone):
    hz = r53.get_hosted_zone(Id=zone["Id"])
    props = {"Name": hz["HostedZone"]["Name"]}
    comment = (hz["HostedZone"].get("Config") or {}).get("Comment")
    if comment:
        props["HostedZoneConfig"] = {"Comment": comment}
    tags = (r53.list_tags_for_resource(ResourceType="hostedzone", ResourceId=zone["Id"])
            .get("ResourceTagSet", {}).get("Tags", []))
    if tags:
        props["HostedZoneTags"] = [{"Key": t["Key"], "Value": t["Value"]} for t in tags]
    if zone["Private"]:
        props["VPCs"] = [{"VPCId": v["VPCId"], "VPCRegion": v["VPCRegion"]} for v in hz.get("VPCs", [])]
    else:
        try:
            qlc = r53.list_query_logging_configs(HostedZoneId=zone["Id"]).get("QueryLoggingConfigs", [])
        except Exception:  # noqa: BLE001
            qlc = []
        if qlc:
            props["QueryLoggingConfig"] = {"CloudWatchLogsLogGroupArn": qlc[0]["CloudWatchLogsLogGroupArn"]}
    return props


# ------------------------------------------------------------------------- main

def file_base(zone):
    return (norm_name(zone["Name"]) or "root") + ("-private" if zone["Private"] else "")


def export_zone_for_import(aws, zone, out, region):
    base = file_base(zone)
    stack = ("dns-" + re.sub(r"[^A-Za-z0-9-]", "-", base))[:128]
    template = {"AWSTemplateFormatVersion": "2010-09-09",
                "Resources": {"HostedZone": {"Type": HOSTEDZONE, "DeletionPolicy": "Retain",
                                             "UpdateReplacePolicy": "Retain",
                                             "Properties": zone_properties(aws.r53, zone)}}}
    tpath, ipath = out / f"{base}-hostedzone.yaml", out / f"{base}-import.json"
    tpath.write_text(to_yaml(template, header=(
        f"Hosted zone {zone['Name']} ({zone['Id']}), for a new stack created by import.\n"
        "Import this first, then run r53_export.py again to export the zone's records.")))
    ipath.write_text(json.dumps([{"ResourceType": HOSTEDZONE, "LogicalResourceId": "HostedZone",
                                  "ResourceIdentifier": {"Id": zone["Id"]}}], indent=2) + "\n")
    print(f"No CloudFormation stack holds {zone['Name']} ({zone['Id']}) yet.")
    print(f"Wrote {tpath} and {ipath}. Import the zone into a new stack (or use IaC generator as before):")
    print(f"  aws cloudformation create-change-set --stack-name {stack} --change-set-name import-hosted-zone "
          f"--change-set-type IMPORT --template-body file://{tpath} --resources-to-import file://{ipath} --region {region}")
    print("  # review the change set, then:")
    print(f"  aws cloudformation execute-change-set --stack-name {stack} --change-set-name import-hosted-zone --region {region}")
    if zone["Private"]:
        print("Private zone: VPCs lists every associated VPC. Remove any whose association is managed from another account.")
    print("Then run this script again to export the records.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zone", help="hosted zone name (example.com) or zone ID (Z0123...)")
    ap.add_argument("--region", help="region of your zone stacks (default: your AWS config)")
    ap.add_argument("--profile")
    ap.add_argument("--regions", default="all",
                    help="regions to check for stacks: 'all' (default) or a,b,c")
    ap.add_argument("--full", action="store_true",
                    help="write the stack's whole template plus the new records")
    ap.add_argument("--max", type=int, metavar="N", help="export at most N new records")
    ap.add_argument("--out", default=".", help="folder for the output files (default: here)")
    args = ap.parse_args(argv)
    if args.max is not None and args.max < 1:
        die("--max must be at least 1")
    try:
        run(args)
    except Exception as e:  # noqa: BLE001
        err = getattr(e, "response", {}).get("Error")
        if not err:
            raise
        die(f"{err.get('Code')}: {err.get('Message')}")


def run(args):
    aws = Aws(args.profile, args.region)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    zone = find_zone(aws.r53, args.zone)
    if zone.get("LinkedService"):
        die(f"{zone['Name']} is managed by {zone['LinkedService'].get('ServicePrincipal')}; leave it alone.")
    zone_owner, record_owners, notes = scan_stacks(aws, regions_to_scan(aws, args.regions))
    for n in notes:
        print(f"  note: {n}")
    owners = zone_owner.get(zone["Id"], [])
    if not owners:
        export_zone_for_import(aws, zone, out, aws.region)
        return
    if len(owners) > 1:
        die(f"{zone['Name']} is in more than one stack: " + ", ".join(f"{o[1]} ({o[0]})" for o in owners))
    region, stack, zone_lid = owners[0]
    template = load_template(aws.cfn(region).get_template(StackName=stack, TemplateStage="Original")["TemplateBody"])
    rrsets = list_records(aws.r53, zone["Id"])
    new, already, skipped, warnings = plan_records(zone, rrsets, template, (region, stack), record_owners)

    print(f"{zone['Name']} ({zone['Id']}) is in stack {stack} ({region}) as {zone_lid}, so the zone is not exported.")
    print(f"Records: {len(rrsets)} in Route 53 | {len(already)} already in this stack | "
          f"{len(skipped)} left out | {len(new)} new")
    for label, reason in skipped:
        print(f"  left out  {label}: {reason}")
    for w in warnings:
        print(f"  check     {w}")
    if not new:
        print("Nothing to export: every record is already in a stack.")
        return

    total_new = len(new)
    if args.max and total_new > args.max:
        new = new[: args.max]
    full = copy.deepcopy(template)
    full.setdefault("Resources", {})
    for lid, res in new:
        full["Resources"][lid] = res
    count = len(new)
    later = f" ({total_new - count} more on the next run, once these are deployed)" if count < total_new else ""
    base = file_base(zone)
    if args.full:
        path = out / f"{base}-stack.yaml"
        body = to_yaml(full, header=(
            f"Stack {stack} ({region}): its current template plus {count} record(s) that no stack managed yet.\n"
            "Deploy as an update to that stack. Record values are copied exactly from Route 53; keep them as they are."))
        path.write_text(body)
        print(f"Wrote {path}: the stack's current template plus {count} new record(s){later}.")
        print(f"Next: deploy it as an update to {stack}.")
    else:
        path = out / f"{base}-records.yaml"
        path.write_text(to_yaml({"Resources": dict(new)}, header=(
            f"{count} record(s) from {zone['Name']} ({zone['Id']}) that no CloudFormation stack manages yet.\n"
            f"Add them under Resources: in the template of stack {stack} ({region}), then update that stack.\n"
            "Do not deploy this file on its own: it would take the hosted zone out of the stack.\n"
            "Values are copied exactly from Route 53; keep them as they are.")))
        print(f"Wrote {path} with {count} new record(s){later}.")
        print(f"Next: add them under Resources: in {stack}'s template, then update the stack.")
    size = len(to_yaml(full).encode())
    if size > BODY_LIMIT:
        print(f"Note: with these records the stack's template is {size:,} bytes. Over 51,200 bytes, "
              "deploy it from S3 (--template-url), not --template-body.")
    if len(full["Resources"]) > STACK_RESOURCE_LIMIT:
        print(f"Note: that is {len(full['Resources'])} resources, over the {STACK_RESOURCE_LIMIT} per stack. "
              "Split this zone's records across two stacks.")


if __name__ == "__main__":
    main()
