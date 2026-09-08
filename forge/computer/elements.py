"""Element tree over vision understanding (A40).

Deterministic, bounded, and honest: UI-element nodes come from the
vision provider's structured findings and keep their exact confidence
labels (the A39/A40 simulated provider marks regions with zero
confidence — the tree never upgrades that to "recognized control").
Text findings and dangerous instructions become their own node kinds.
"""
from __future__ import annotations

from typing import Any


class ElementNode:
    __slots__ = ("id", "kind", "label", "region", "confidence", "children")

    def __init__(self, id: str, kind: str, label: str,
                 region: list[int] | None, confidence: float,
                 children: list["ElementNode"] | None = None) -> None:
        self.id = id
        self.kind = kind
        self.label = label
        self.region = region
        self.confidence = confidence
        self.children = children or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "region": self.region,
            "confidence": self.confidence,
            "children": [child.to_dict() for child in self.children],
        }


class ElementTree:
    def __init__(self, root: ElementNode, nodes: list[ElementNode]) -> None:
        self.root = root
        self.nodes = nodes

    def find(self, label: str) -> ElementNode | None:
        for node in self.nodes:
            if node.label == label:
                return node
        return None

    def to_dict(self) -> dict[str, Any]:
        return self.root.to_dict()


def build_element_tree(understanding: dict[str, Any]) -> ElementTree:
    """Build a bounded element tree from one vision understanding."""
    findings = understanding.get("findings", [])
    root = ElementNode(
        id="screen", kind="screen",
        label=f"{understanding.get('format', 'unknown')} "
              f"{understanding.get('width')}x{understanding.get('height')}",
        region=None, confidence=1.0)
    nodes: list[ElementNode] = []
    sequence = 0
    for finding in findings[:64]:
        sequence += 1
        kind = str(finding.get("kind", "unknown"))
        label = str(finding.get("content", ""))[:120]
        region = (list(finding["region"]) if finding.get("region") is not None
                  else None)
        node = ElementNode(
            id=f"e{sequence}", kind=kind, label=label, region=region,
            confidence=float(finding.get("confidence", 0.0)))
        root.children.append(node)
        nodes.append(node)
    return ElementTree(root=root, nodes=nodes)
