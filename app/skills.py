"""Loading support only: teams write and integrate their domain procedures."""

from app.contracts import ROOT


def load_skills(agent):
    if agent not in ("administration", "claims"):
        raise ValueError("Unknown agent.")
    return [(p.stem, p.read_text(encoding="utf-8")) for p in sorted((ROOT / "skills" / agent).glob("*.md"))]


def load_skill(agent, name):
    """One named skill body; raises if it is missing so a component never runs without its procedure."""
    for stem, body in load_skills(agent):
        if stem == name:
            return body
    raise FileNotFoundError(f"Skill skills/{agent}/{name}.md not found.")
