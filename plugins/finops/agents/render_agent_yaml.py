#!/usr/bin/env python3
"""Render finops-investigator.json as portal-ready ExtendedAgent YAML.

SRE Agent plugins carry only skills and MCP server requirements, so a plugin install never creates
the finops-investigator sub-agent. When install-api.sh cannot run, paste this YAML into
Builder > Agent Canvas > Create > Custom agent > YAML (or apply it with the Azure MCP
`sreagent_workflows_apply` command).

    python render_agent_yaml.py --agent-resource-id /subscriptions/.../agents/<name>   # print
    python render_agent_yaml.py write    # refresh the committed template (placeholder id)
    python render_agent_yaml.py check    # fail if the committed template is stale
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "finops-investigator.json"
TEMPLATE = HERE / "finops-investigator.yaml"
PLACEHOLDER = "{{AGENT_RESOURCE_ID}}"

# Spec fields emitted in this order; names match the agent's v2 ExtendedAgent API.
FIELDS = (
    "instructions",
    "handoffDescription",
    "handoffs",
    "tools",
    "mcpTools",
    "connectors",
    "allowParallelToolCalls",
    "maxReflectionCount",
    "disableDocumentRetrieval",
    "enableSkills",
    "addSystemSkills",
    "allowedSkills",
    "outputType",
)


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def render(agent_resource_id=PLACEHOLDER):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    props = manifest["properties"]
    lines = [
        "# Generated from finops-investigator.json by render_agent_yaml.py - do not edit by hand.",
        "api_version: azuresre.ai/v2",
        "kind: ExtendedAgent",
        "metadata:",
        f"  name: {manifest['name']}",
        "spec:",
    ]
    for field in FIELDS:
        if field not in props:
            continue
        value = props[field]
        if field == "instructions":
            text = value.replace(PLACEHOLDER, agent_resource_id)
            lines.append("  instructions: |-")
            lines.extend(f"    {line}" if line else "" for line in text.splitlines())
        elif isinstance(value, list):
            if not value:
                lines.append(f"  {field}: []")
            else:
                lines.append(f"  {field}:")
                lines.extend(f"    - {_scalar(item)}" for item in value)
        else:
            lines.append(f"  {field}: {_scalar(value)}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", choices=("print", "write", "check"), default="print")
    parser.add_argument("--agent-resource-id", default=PLACEHOLDER,
                        help="full ARM id of the SRE Agent; substituted into the instructions")
    args = parser.parse_args(argv)

    if args.mode == "print":
        if args.agent_resource_id != PLACEHOLDER and not args.agent_resource_id.lower().startswith("/subscriptions/"):
            parser.error("--agent-resource-id must be a full ARM resource id")
        sys.stdout.write(render(args.agent_resource_id))
        return 0
    expected = render()
    if args.mode == "write":
        TEMPLATE.write_text(expected, encoding="utf-8", newline="\n")
        print(f"wrote {TEMPLATE.name}")
        return 0
    current = TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n") if TEMPLATE.exists() else ""
    if current != expected:
        print(f"{TEMPLATE.name} is stale; run: python render_agent_yaml.py write", file=sys.stderr)
        return 1
    print(f"{TEMPLATE.name} is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
