"""CloudFormation-aware template helpers.

* Parses YAML templates that use short-form intrinsic functions (!Ref, !Sub, !GetAtt ...).
* Slices the *raw text* of one resource out of the template, so drift reports and
  Amazon Q prompts show the code exactly as it sits in the repo.
* Loads and validates params.json.
"""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import Any

import yaml


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that understands CloudFormation short-form tags."""


# CloudFormation keeps "2010-09-09" as a string, so drop PyYAML's timestamp resolver.
CfnLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_intrinsic(loader: yaml.Loader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    if suffix == "Ref":
        return {"Ref": value}
    if suffix == "Condition":
        return {"Condition": value}
    if suffix == "GetAtt" and isinstance(value, str):
        return {"Fn::GetAtt": value.split(".", 1)}
    return {f"Fn::{suffix}": value}


CfnLoader.add_multi_constructor("!", _construct_intrinsic)


class TemplateError(Exception):
    """Raised for template / params problems the user must fix."""


def load_template_text(text: str) -> dict:
    try:
        data = yaml.load(text, Loader=CfnLoader)  # noqa: S506 - CfnLoader extends SafeLoader
    except yaml.YAMLError as exc:
        raise TemplateError(f"Template is not valid YAML/JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TemplateError("Template must be a YAML/JSON mapping at the top level.")
    return data


def load_template(path: str | Path) -> dict:
    return load_template_text(Path(path).read_text(encoding="utf-8"))


def section_snippet(text: str, section: str) -> str | None:
    """Raw text of a top-level section (e.g. Parameters)."""
    try:
        root = yaml.compose(text, Loader=CfnLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return None
    lines = text.splitlines()
    for key_node, value_node in root.value:
        if key_node.value == section:
            return _slice(lines, key_node, value_node)
    return None


def resource_snippet(text: str, logical_id: str, section: str = "Resources") -> str | None:
    """Raw text of one resource block, starting at its logical ID (dedented to column 0)."""
    try:
        root = yaml.compose(text, Loader=CfnLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return None
    lines = text.splitlines()
    for key_node, value_node in root.value:
        if key_node.value != section or not isinstance(value_node, yaml.MappingNode):
            continue
        for res_key, res_value in value_node.value:
            if res_key.value == logical_id:
                return _slice(lines, res_key, res_value)
    return None


def _slice(lines: list[str], key_node: yaml.Node, value_node: yaml.Node) -> str:
    start = key_node.start_mark.line
    end_line, end_col = value_node.end_mark.line, value_node.end_mark.column
    if end_line == start:
        chunk = lines[start:start + 1]
    else:
        last = end_line
        if end_col > 0 and end_line < len(lines) and lines[end_line][:end_col].strip():
            last = end_line + 1
        chunk = lines[start:last]
    while chunk and (not chunk[-1].strip() or chunk[-1].strip().startswith("#")):
        chunk.pop()
    return textwrap.dedent("\n".join(chunk))


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def describe_template(template: dict) -> dict:
    params = template.get("Parameters") or {}
    resources = template.get("Resources") or {}
    return {
        "parameters": {
            name: {
                "type": spec.get("Type", "String"),
                "has_default": "Default" in spec,
                "default": spec.get("Default"),
                "no_echo": _truthy(spec.get("NoEcho", False)),
                "allowed_values": spec.get("AllowedValues"),
                "allowed_pattern": spec.get("AllowedPattern"),
                "min_length": spec.get("MinLength"),
                "max_length": spec.get("MaxLength"),
                "min_value": spec.get("MinValue"),
                "max_value": spec.get("MaxValue"),
            }
            for name, spec in params.items() if isinstance(spec, dict)
        },
        "resource_count": len(resources),
        "output_count": len(template.get("Outputs") or {}),
        "resource_types": sorted({str(r.get("Type")) for r in resources.values() if isinstance(r, dict)}),
        "nested_stacks": sorted(k for k, r in resources.items()
                                if isinstance(r, dict) and r.get("Type") == "AWS::CloudFormation::Stack"),
        "transform": template.get("Transform"),
        "description": template.get("Description", ""),
    }


def _as_param_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ",".join(_as_param_string(v) for v in value)
    if value is None:
        return ""
    return str(value)


def load_params(path: str | Path) -> list[dict]:
    """Read params.json (CloudFormation CLI format) into boto3 Parameters.

    Values are passed to the API as-is through boto3, so spaces and commas inside values
    no longer break deployments the way shell word-splitting did in the old pipeline.
    """
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise TemplateError(f"{source} must be a JSON array of {{ParameterKey, ParameterValue}} objects.")
    seen: set[str] = set()
    params: list[dict] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or not item.get("ParameterKey"):
            raise TemplateError(f"{source}: entry #{index + 1} has no ParameterKey.")
        key = str(item["ParameterKey"])
        if key in seen:
            raise TemplateError(f"{source}: ParameterKey '{key}' appears more than once.")
        seen.add(key)
        if item.get("UsePreviousValue") is True:
            params.append({"ParameterKey": key, "UsePreviousValue": True})
            continue
        if "ParameterValue" not in item:
            raise TemplateError(f"{source}: '{key}' has no ParameterValue.")
        params.append({"ParameterKey": key, "ParameterValue": _as_param_string(item["ParameterValue"])})
    return params


def check_params(params: list[dict], template_info: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) comparing params.json against the template Parameters."""
    errors: list[str] = []
    warnings: list[str] = []
    declared = template_info["parameters"]
    supplied = {p["ParameterKey"]: p for p in params}

    for key in supplied:
        if key not in declared:
            errors.append(f"params.json sets '{key}', but the template has no such parameter "
                          "(CloudFormation rejects unknown parameters).")
    for key, spec in declared.items():
        if key not in supplied and not spec["has_default"]:
            errors.append(f"Template parameter '{key}' has no Default and is missing from params.json.")

    for key, item in supplied.items():
        spec = declared.get(key)
        if not spec or "ParameterValue" not in item:
            continue
        value = item["ParameterValue"]
        ptype = str(spec["type"])
        is_list = "List" in ptype or ptype == "CommaDelimitedList"
        values = [v.strip() for v in value.split(",")] if is_list else [value]
        if spec["allowed_values"]:
            allowed = {_as_param_string(v) for v in spec["allowed_values"]}
            bad = [v for v in values if v not in allowed]
            if bad:
                errors.append(f"'{key}' value {bad} is not in AllowedValues {sorted(allowed)}.")
        if spec["allowed_pattern"] and ptype == "String":
            try:
                if not re.fullmatch(str(spec["allowed_pattern"]), value):
                    warnings.append(f"'{key}' value may not match AllowedPattern {spec['allowed_pattern']}.")
            except re.error:
                pass
        if ptype == "String":
            if spec["min_length"] is not None and len(value) < int(spec["min_length"]):
                errors.append(f"'{key}' is shorter than MinLength {spec['min_length']}.")
            if spec["max_length"] is not None and len(value) > int(spec["max_length"]):
                errors.append(f"'{key}' is longer than MaxLength {spec['max_length']}.")
        if ptype == "Number":
            try:
                number = float(value)
            except ValueError:
                errors.append(f"'{key}' must be a number (got '{value}').")
                continue
            if spec["min_value"] is not None and number < float(spec["min_value"]):
                errors.append(f"'{key}' is below MinValue {spec['min_value']}.")
            if spec["max_value"] is not None and number > float(spec["max_value"]):
                errors.append(f"'{key}' is above MaxValue {spec['max_value']}.")
    return errors, warnings


def no_echo_values(params: list[dict], template_info: dict) -> dict[str, str]:
    declared = template_info["parameters"]
    return {p["ParameterKey"]: p.get("ParameterValue", "") for p in params
            if declared.get(p["ParameterKey"], {}).get("no_echo")}


def display_params(params: list[dict], template_info: dict) -> list[dict]:
    """params.json with NoEcho values masked — safe for reports, emails and AI prompts."""
    hidden = no_echo_values(params, template_info)
    shown = []
    for p in params:
        value = "****" if p["ParameterKey"] in hidden else p.get("ParameterValue", "<previous value>")
        shown.append({"ParameterKey": p["ParameterKey"], "ParameterValue": value})
    return shown
