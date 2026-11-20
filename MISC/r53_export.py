#!/usr/bin/env python3
"""
r53_export.py - export ONE Route 53 hosted zone and its records as a CloudFormation
template to import, leaving out whatever CloudFormation already manages.

    python3 r53_export.py example.com --region eu-west-1 [--profile myprofile]

1. Finds the hosted zone (name or zone ID) and checks every enabled region for a
   CloudFormation stack that already holds it (or only --regions a,b).
2. Builds one template:
   - A stack already holds the zone: that stack's current template, hosted zone
     included as it is, plus every record no stack manages yet.
   - No stack holds the zone: the hosted zone plus all its records, so a single
     import creates the stack.
   Records include the zone's own NS and SOA. Records another stack already
   manages, and records made by a traffic policy, are left out.
3. Writes three files:
     <zone>-import-template.yaml  the template. Existing stack: Stack actions >
                                  Import resources into stack. New stack: Create
                                  stack > With existing resources (import resources).
     <zone>-import-values.csv     one row per resource to import, in template order,
                                  with the identifier values to copy into the
                                  console: Id for the hosted zone; Name, HostedZoneId,
                                  Type and SetIdentifier for records.
     <zone>-import.json           the same values for the CLI, so nothing is typed.
   --max N      puts only the first N records in this import; run again
                afterwards for the next N.
   --name NAME  only records with this name (repeatable). Good for a first test
                with one simple record, or for importing one record later.

Logical IDs are the name with everything except letters and digits taken out,
then the type: www.example.com. CNAME -> wwwexamplecomCNAME, *.example.com. A ->
WILDCARDexamplecomA, and the zone itself -> examplecomHostedZone. CloudFormation
allows only letters and digits in a logical ID, so the exact names are in the CSV.

Everything carries DeletionPolicy and UpdateReplacePolicy Retain: an import needs a
DeletionPolicy, and Retain means deleting the stack never removes the live zone or
records. Values are copied exactly as Route 53 returns them, so drift stays clean.

Requires Python 3.8+, boto3 and PyYAML:  pip install boto3 pyyaml
"""

import argparse
import collections
import copy
import csv
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


def logical_id(name, rtype, set_id=""):
    """The record name with only letters and digits kept, then the type in capitals,
    then the set identifier (letters and digits) when there is one."""
    flat = re.sub(r"[^A-Za-z0-9]", "", decode_name(name).replace("*", "WILDCARD"))
    lid = flat + re.sub(r"[^A-Za-z0-9]", "", rtype).upper() + re.sub(r"[^A-Za-z0-9]", "", set_id or "")
    if len(lid) > 240:
        lid = lid[:240] + hashlib.sha1(lid.encode()).hexdigest()[:8].upper()
    return lid or "Record"


def zone_logical_id(zone_name):
    flat = re.sub(r"[^A-Za-z0-9]", "", decode_name(zone_name))
    return (flat[:240] or "Zone") + "HostedZone"


# --------------------------------------------------------- records -> template

def skip_reason(rr):
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
    """Split the zone's records into new / already managed / left out.

    template       the zone stack's current template (dict)
    own_stack      (region, stack name) of the zone's stack
    record_owners  {norm_name: [(region, stack, logical_id), ...]} from every stack
    Returns (new [(logical_id, resource)] sorted by logical ID,
             already [(label, logical_id)], skipped [(label, reason)], warnings [text]).
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

    already, skipped, warnings, todo = [], [], [], []
    for rr in rrsets:
        sid = rr.get("SetIdentifier", "")
        key = (norm_name(rr["Name"]), rr["Type"], sid)
        label = f"{rr['Name']} {rr['Type']}" + (f" [{sid}]" if sid else "")
        reason = skip_reason(rr)
        if reason:
            skipped.append((label, reason))
            continue
        if key in managed:
            already.append((label, managed[key]))
            continue
        # Owned by another stack, or by a record in this stack whose name is computed.
        others = [o for o in record_owners.get(key[0], [])
                  if (o[0], o[1]) != own_stack or o[2] not in literal_ids]
        if others:
            owners = sorted({f"stack {o[1]} ({o[0]})" for o in others})
            skipped.append((label, "already managed by " + ", ".join(owners)))
            continue
        warnings += [f"{label}: {w}" for w in record_warnings(rr)]
        todo.append((key, logical_id(rr["Name"], rr["Type"], sid), rrset_to_props(rr, zone["Id"])))

    # Two records can flatten to the same ID (my-app and myapp); both then get a short
    # hash so neither name wins by luck. IDs already in the stack are avoided too.
    counts = collections.Counter(lid for _, lid, _ in todo)
    used, new = set(resources), []
    for key, lid, props in todo:
        if counts[lid] > 1 or lid in used:
            lid = lid + "X" + hashlib.sha1("|".join(key).encode()).hexdigest()[:6].upper()
        while lid in used:
            lid += "X"
        used.add(lid)
        new.append((lid, {"Type": RECORDSET, "DeletionPolicy": "Retain",
                          "UpdateReplacePolicy": "Retain", "Properties": props}))
    new.sort(key=lambda t: (t[0].lower(), t[0]))
    return new, already, skipped, warnings


CSV_FIELDS = ["LogicalId", "ResourceType", "Id", "Name", "HostedZoneId", "Type", "SetIdentifier"]


def import_rows(new, zone_entry=None):
    """Identifier values for every resource to import, in template order.
    zone_entry = (logical_id, zone_id) when the hosted zone is imported too."""
    rows = []
    if zone_entry:
        rows.append({"LogicalId": zone_entry[0], "ResourceType": HOSTEDZONE, "Id": zone_entry[1],
                     "Name": "", "HostedZoneId": "", "Type": "", "SetIdentifier": ""})
    for lid, res in new:
        p = res["Properties"]
        rows.append({"LogicalId": lid, "ResourceType": RECORDSET, "Id": "", "Name": p["Name"],
                     "HostedZoneId": p["HostedZoneId"], "Type": p["Type"],
                     "SetIdentifier": p.get("SetIdentifier", "")})
    return rows


def resources_to_import(rows):
    out = []
    for r in rows:
        if r["ResourceType"] == HOSTEDZONE:
            ident = {"Id": r["Id"]}
        else:
            ident = {"HostedZoneId": r["HostedZoneId"], "Name": r["Name"], "Type": r["Type"]}
            if r["SetIdentifier"]:
                ident["SetIdentifier"] = r["SetIdentifier"]
        out.append({"ResourceType": r["ResourceType"], "LogicalResourceId": r["LogicalId"],
                    "ResourceIdentifier": ident})
    return out


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zone", help="hosted zone name (example.com) or zone ID (Z0123...)")
    ap.add_argument("--region", help="region of your zone stacks (default: your AWS config)")
    ap.add_argument("--profile")
    ap.add_argument("--regions", default="all",
                    help="regions to check for stacks: 'all' (default) or a,b,c")
    ap.add_argument("--max", type=int, metavar="N", help="put at most N records in this import")
    ap.add_argument("--name", action="append", default=[], metavar="NAME",
                    help="only records with this name (repeatable)")
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
    if len(owners) > 1:
        die(f"{zone['Name']} is in more than one stack: " + ", ".join(f"{o[1]} ({o[0]})" for o in owners))
    base = file_base(zone)
    if owners:
        region, stack, zone_lid = owners[0]
        own_stack, zone_entry = (region, stack), None
        template = load_template(aws.cfn(region).get_template(StackName=stack, TemplateStage="Original")["TemplateBody"])
        print(f"{zone['Name']} ({zone['Id']}) is already in stack {stack} ({region}) as {zone_lid}; "
              "the template keeps it as it is.")
    else:
        region, stack = aws.region, ("dns-" + re.sub(r"[^A-Za-z0-9-]", "-", base))[:128]
        zone_lid = zone_logical_id(zone["Name"])
        own_stack, zone_entry = None, (zone_lid, zone["Id"])
        template = {"AWSTemplateFormatVersion": "2010-09-09",
                    "Resources": {zone_lid: {"Type": HOSTEDZONE, "DeletionPolicy": "Retain",
                                             "UpdateReplacePolicy": "Retain",
                                             "Properties": zone_properties(aws.r53, zone)}}}
        print(f"No CloudFormation stack holds {zone['Name']} ({zone['Id']}) yet, so the hosted zone goes "
              f"into the template as {zone_lid}.")
        if zone["Private"]:
            print("  Private zone: VPCs lists every associated VPC. Remove any whose association is managed "
                  "from another account.")
    rrsets = list_records(aws.r53, zone["Id"])
    if args.name:
        wanted = {norm_name(n) for n in args.name}
        rrsets = [rr for rr in rrsets if norm_name(rr["Name"]) in wanted]
        if not rrsets:
            die(f"No records named {', '.join(args.name)} in {zone['Name']}")
        print(f"Only records named {', '.join(sorted(wanted))}.")
    new, already, skipped, warnings = plan_records(zone, rrsets, template, own_stack, record_owners)

    print(f"Records: {len(rrsets)} in Route 53 | {len(already)} already in this stack | "
          f"{len(skipped)} left out | {len(new)} to import")
    for label, reason in skipped:
        print(f"  left out  {label}: {reason}")
    for w in warnings:
        print(f"  check     {w}")
    if not new and not zone_entry:
        print("Nothing to import: every record is already in a stack.")
        return

    total = len(new)
    if args.max and total > args.max:
        new = new[: args.max]
    count = len(new)
    full = copy.deepcopy(template)
    full.setdefault("Resources", {})
    for lid, res in new:
        full["Resources"][lid] = res
    rows = import_rows(new, zone_entry)

    tpath = out / f"{base}-import-template.yaml"
    cpath = out / f"{base}-import-values.csv"
    jpath = out / f"{base}-import.json"
    if zone_entry:
        header = (f"New stack for hosted zone {zone['Name']} ({zone['Id']}): the zone plus {count} record(s) to import.\n"
                  "Use it with Create stack > With existing resources (import resources), or the CLI command\n"
                  f"r53_export.py printed. Each resource's identifier values are in {cpath.name}.")
    else:
        header = (f"Stack {stack} ({region}): its current template plus {count} Route 53 record(s) to import.\n"
                  "Use it with Stack actions > Import resources into stack, or the CLI command r53_export.py printed,\n"
                  f"not as a normal update. Each record's identifier values are in {cpath.name}.")
    tpath.write_text(to_yaml(full, header=header))
    with open(cpath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    jpath.write_text(json.dumps(resources_to_import(rows), indent=2) + "\n")

    later = f" ({total - count} more records on the next run, once these are imported)" if count < total else ""
    what = f"the hosted zone and {count} record(s)" if zone_entry else f"{count} record(s)"
    print(f"Wrote {what} to import{later}:")
    print(f"  {tpath}   the template")
    print(f"  {cpath}   identifier values for the console, in the same order")
    print(f"  {jpath}   the same values for the CLI")
    if zone_entry:
        print(f"Console: Create stack > With existing resources (import resources) > upload {tpath.name},")
        print(f"         fill each resource's boxes from {cpath.name} (the hosted zone needs only its Id),")
        print(f"         and name the stack, for example {stack}.")
    else:
        print(f"Console: stack {stack} > Stack actions > Import resources into stack > upload {tpath.name},")
        print(f"         then fill each record's boxes from {cpath.name}.")
    print("         Leave SetIdentifier empty where the CSV has none.")
    print("CLI, nothing to type:")
    print(f"  aws cloudformation create-change-set --stack-name {stack} --change-set-name import-records "
          f"--change-set-type IMPORT --template-body file://{tpath} --resources-to-import file://{jpath} --region {region}")
    print("  # review the change set in the console and execute it there")
    if any(r["Type"] in ("NS", "SOA") and norm_name(r["Name"]) == norm_name(zone["Name"]) for r in rows):
        print("Includes the zone's own NS and SOA records. Keep their DeletionPolicy as Retain: Route 53 won't delete them.")
    size = len(to_yaml(full).encode())
    if size > BODY_LIMIT:
        print(f"Note: the template is {size:,} bytes. The console upload handles that; for the CLI, put it in S3 "
              "and use --template-url instead of --template-body.")
    if len(full["Resources"]) > STACK_RESOURCE_LIMIT:
        print(f"Note: that is {len(full['Resources'])} resources, over the {STACK_RESOURCE_LIMIT} per stack. "
              "Split this zone's records across two stacks.")


if __name__ == "__main__":
    main()
