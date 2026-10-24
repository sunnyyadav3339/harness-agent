from datetime import timedelta

import pytest

from harness.skills.loader import SkillError, SkillLibrary, load_vault, parse_skill_text
from harness.skills.reflector import append_heuristics, existing_heuristics, save_draft_skill
from harness.timeutil import CLOCK

SKILL = """---
name: demo_skill
description: Book a demo widget on the demo site
triggers: ["demo widget", "book widget"]
data: ["data/info.yaml"]
---
# Demo
## Steps
1. do it

```yaml macros
go:
  description: open the page
  steps:
    - {tool: browser.open, args: {url: "https://example.com/{{param.page}}"}}
```

## Learned Heuristics (auto-updated)
- [Learned]: existing tip

## Appendix
keep me
"""


def _write(ws, name, text):
    d = ws.skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d / "SKILL.md"


def test_parse_macros_and_match(workspace):
    _write(workspace, "demo_skill", SKILL)
    _write(workspace, "other", "---\nname: other\ndescription: Organise files in folders\ntriggers: [rename]\n---\nbody")
    lib = SkillLibrary(workspace.skills_dir)
    skill, ranking = lib.match("please book widget for me")
    assert skill.name == "demo_skill"
    assert skill.macros["go"]["steps"][0]["tool"] == "browser.open"
    assert lib.match("what is the weather")[0] is None


def test_broken_skill_is_skipped_not_fatal(workspace):
    _write(workspace, "good", SKILL)
    _write(workspace, "bad", "no frontmatter at all")
    names = [s.name for s in SkillLibrary(workspace.skills_dir).all()]
    assert names == ["demo_skill"]
    with pytest.raises(SkillError):
        parse_skill_text("---\nname: x\n---\n", workspace.skills_dir / "x.md")


def test_vault_dynamic_dates(workspace):
    (workspace.data_dir / "info.yaml").write_text('when: "${date+1:%d/%m/%Y}"\nplain: 5\n', encoding="utf-8")
    skill = parse_skill_text(SKILL, workspace.skills_dir / "demo_skill" / "SKILL.md")
    vault = load_vault(workspace.workspace, skill)
    assert vault["info"]["when"] == (CLOCK.now() + timedelta(days=1)).strftime("%d/%m/%Y")
    assert vault["info"]["plain"] == 5


def test_append_heuristics_backs_up_and_keeps_sections(workspace):
    path = _write(workspace, "demo_skill", SKILL)
    added = append_heuristics(path, ["click the second tab first", "existing tip"])
    text = path.read_text(encoding="utf-8")
    assert added == 1  # "existing tip" is deduplicated
    assert "click the second tab first" in text
    assert text.index("click the second tab first") < text.index("## Appendix")
    assert "keep me" in text
    assert len(existing_heuristics(text)) == 2
    assert list((path.parent / "history").glob("SKILL_*.md"))
    parse_skill_text(text, path)  # still valid


def test_save_draft_skill_validates_and_never_overwrites(workspace):
    md = "---\nname: New Thing\ndescription: does a thing\ntriggers: [thing]\ndraft: true\n---\n# T\n"
    p1 = save_draft_skill(workspace.skills_dir, md)
    p2 = save_draft_skill(workspace.skills_dir, md)
    assert p1.parent.name == "new_thing" and p2.parent.name == "new_thing_2"
    with pytest.raises(SkillError):
        save_draft_skill(workspace.skills_dir, "not a skill")
