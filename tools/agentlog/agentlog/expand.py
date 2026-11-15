# Copyright (c) 2026 Andrew Murphy
#
# This file is part of coding-agent-useful-tools.
#
# coding-agent-useful-tools is dual-licensed:
# 1. Under the GNU Affero General Public License v3.0 (AGPLv3) for open-source
#    and non-proprietary use. See the LICENSE file in the root directory.
# 2. Under a Commercial License for proprietary use or without AGPLv3 
#    obligations. See COMMERCIAL-LICENSE.md or contact andrew_murphy_datapro@outlook.com
#     with subject line: "Commercial Licensing Request for coding-agent-useful-tools".

"""Reconstruct redacted normalized events from a lossless compression report."""
import json
from pathlib import Path

from .core.normalize import VolatileValues, restore_volatile
from .core.out import summary
from .core.paths import checked_destination
from .core.redact import redact_object
from .readers.base import Event, Usage


def _definitions(data):
    """Resolve and validate the explicitly ordered B/V ancestry graph.

    Full bodies make reconstruction independent of a diff's display cap. The
    parent links are still validated rather than guessed from event order or
    reference numbers. Legacy schema-v1 reports without variants remain valid.
    """
    blocks = data["blocks"]
    variants = data.get("variants", {})
    if not isinstance(blocks, dict) or not isinstance(variants, dict):
        raise ValueError("Invalid body dictionaries")
    entries = dict(blocks)
    if entries.keys() & variants.keys():
        raise ValueError("Duplicate body reference")
    entries.update(variants)
    if variants:
        orders = set()
        for ref, entry in entries.items():
            expected = "V" if ref in variants else "B"
            if (not isinstance(ref, str) or not ref.startswith(expected) or
                    not ref[1:].isdigit() or not isinstance(entry, dict) or entry.get("ref") != ref):
                raise ValueError("Invalid body reference")
            order = entry.get("creation_order")
            if type(order) is not int or order < 1 or order in orders:
                raise ValueError("Invalid body creation order")
            orders.add(order)
        for ref, entry in variants.items():
            parent = entries.get(entry.get("parent"))
            if parent is None or parent["creation_order"] >= entry["creation_order"]:
                raise ValueError("Variant parent must identify an earlier body")
            if (entry["kind"], entry["tool"]) != (parent["kind"], parent["tool"]):
                raise ValueError("Variant parent kind and tool must match")
    return entries


def expand_events(data):
    """Yield Events in original order, including empty bookkeeping records."""
    if data.get("schema_version") != 1 or not data.get("lossless"):
        raise ValueError("expand requires a schema-version-1 lossless compress.json")
    try:
        definitions = _definitions(data)
        for occurrence in data["occurrences"]:
            text = []
            for unit in occurrence["units"]:
                definition = definitions[unit["block"]]
                # Keep reading older v1 lossless reports whose near bodies
                # lived on units, but new reports store them only once.
                masked = unit.get("masked_text", definition["masked_text"])
                if not isinstance(masked, str):
                    raise ValueError("Invalid normalized body")
                if "parent" in definition and unit.get("parent") != definition["parent"]:
                    raise ValueError("Occurrence variant parent does not match its definition")
                values = VolatileValues()
                values.extend(unit["values"])
                values.spans = [tuple(span) for span in unit["spans"]]
                if len(values) != len(values.spans):
                    raise ValueError("Mismatched mask values and spans")
                previous = 0
                for start, end in values.spans:
                    if not 0 <= previous <= start < end <= len(masked):
                        raise ValueError("Invalid volatile mask span")
                    previous = end
                text.append(restore_volatile(masked, values))
            fields = {key: value for key, value in occurrence.items()
                      if key in Event.__dataclass_fields__ and key != "text"}
            if fields.get("usage") is not None:
                fields["usage"] = Usage(**fields["usage"])
            yield Event(text=occurrence.get("marker_text", "".join(text)), **fields)
    except (KeyError, TypeError, IndexError) as error:
        raise ValueError("Invalid lossless compression report") from error


def _run(args):
    source = Path(args.report)
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    inputs = [source, data.get("session", {}).get("path", "")]
    target = checked_destination(args.out, inputs=inputs)
    if Path(args.out).is_symlink() or (target.exists() and target.stat().st_nlink > 1):
        raise ValueError("Expanded output must not be a symlink or hard link")
    # Validate the entire report before opening the output, so a malformed
    # report cannot truncate an existing file and leave a partial result.
    from dataclasses import asdict
    count = sum(1 for _ in expand_events(data))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as output:
        for event in expand_events(data):
            output.write(json.dumps(redact_object(asdict(event)), ensure_ascii=False, sort_keys=True) + "\n")
    summary({"events": count, "out": str(target)}, json_mode=args.json)
    return 0


def register(subparsers):
    parser = subparsers.add_parser("expand", help="Rebuild redacted events from lossless compress.json")
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="Destination events.jsonl file")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_run)
