import pytest

from reviewer.config import Settings
from reviewer.context import ReviewContext, annotate_patch, build_context
from reviewer.postprocess import (
    REVIEW_MARKER,
    diff_positions,
    plan_review,
    render_comment,
)
from reviewer.schema import Finding, ReviewResult
from tests.conftest import SAMPLE_PATCH, make_file, make_pr

# --- diff position mapping -------------------------------------------------


def test_single_hunk_new_file():
    assert diff_positions("@@ -0,0 +1,3 @@\n+a\n+b\n+c") == {1: 1, 2: 2, 3: 3}


def test_single_hunk_with_offset_start():
    patch = "@@ -40,3 +42,3 @@ def f():\n ctx\n-old\n+new\n ctx2"
    assert diff_positions(patch) == {42: 1, 43: 3, 44: 4}


def test_multi_hunk_counts_later_headers_and_removed_lines():
    # Line by line: hdr1, ctx(1), del, add(2), add(3), ctx(4), ctx(5), hdr2 (pos 7),
    # ctx(21), del, add(22), add(23).
    assert diff_positions(SAMPLE_PATCH) == {
        1: 1,
        2: 3,
        3: 4,
        4: 5,
        5: 6,
        21: 8,
        22: 10,
        23: 11,
    }


def test_removed_lines_are_not_commentable():
    positions = diff_positions("@@ -1,3 +1,1 @@\n-a\n-b\n c")
    assert positions == {1: 3}


def test_no_newline_marker_occupies_a_position():
    patch = "@@ -1,2 +1,3 @@\n a\n-b\n\\ No newline at end of file\n+b\n+c"
    assert diff_positions(patch) == {1: 1, 2: 4, 3: 5}


def test_three_hunks_positions_keep_increasing():
    patch = "@@ -1 +1 @@\n-a\n+A\n@@ -10 +10 @@\n-j\n+J\n@@ -20 +20 @@\n-t\n+T"
    assert diff_positions(patch) == {1: 2, 10: 5, 20: 8}


def test_pure_rename_without_patch_has_no_positions():
    assert diff_positions(None) == {}
    assert diff_positions("") == {}


def test_positions_agree_with_prompt_line_labels():
    """The line numbers shown to the model must be exactly the mappable lines."""
    labels = (ln[:6].strip() for ln in annotate_patch(SAMPLE_PATCH).splitlines())
    labelled = {int(label) for label in labels if label.isdigit()}
    assert labelled == set(diff_positions(SAMPLE_PATCH))


# --- filtering, dedupe, cap ----------------------------------------------


def finding(**kw) -> Finding:
    base = dict(
        file="src/app.py",
        line=2,
        severity="major",
        category="bug",
        comment="Something is wrong.",
        confidence=0.9,
    )
    return Finding(**{**base, **kw})


def plan_for(findings, ctx=None, **settings):
    ctx = ctx or build_context(make_pr(), Settings())
    return plan_review(ReviewResult(summary="s", findings=findings), ctx, Settings(**settings))


def reasons(plan):
    return [(d.finding.comment, d.reason) for d in plan.dropped]


def test_confidence_threshold_is_inclusive():
    plan = plan_for(
        [finding(confidence=0.7, comment="keep"), finding(confidence=0.69, comment="low", line=3)]
    )
    assert [c.finding.comment for c in plan.comments] == ["keep"]
    assert reasons(plan) == [("low", "low_confidence")]


def test_lines_and_files_outside_diff_are_dropped():
    plan = plan_for(
        [
            finding(line=2, comment="ok"),
            finding(line=6, comment="unchanged region"),
            finding(file="other.py", comment="unknown file"),
        ]
    )
    assert [c.finding.comment for c in plan.comments] == ["ok"]
    assert sorted(r for _, r in reasons(plan)) == ["not_in_diff", "not_in_diff"]


def test_comment_maps_to_position():
    plan = plan_for([finding(line=22)])
    assert plan.comments[0].to_github()["position"] == 10
    assert plan.comments[0].to_github()["path"] == "src/app.py"


def test_renamed_file_uses_new_path():
    patch = "@@ -10,3 +10,3 @@\n ctx\n-old\n+new\n ctx2"
    pr = make_pr(
        files=[
            make_file(
                "pkg/new_name.py",
                patch=patch,
                status="renamed",
                previous_filename="pkg/old_name.py",
            )
        ]
    )
    ctx = build_context(pr, Settings())
    plan = plan_for(
        [
            finding(file="pkg/new_name.py", line=11, comment="new path"),
            finding(file="pkg/old_name.py", line=11, comment="old path"),
        ],
        ctx=ctx,
    )
    assert [c.to_github() for c in plan.comments][0]["position"] == 3
    assert [c.finding.comment for c in plan.comments] == ["new path"]
    assert reasons(plan) == [("old path", "not_in_diff")]


def test_pure_rename_findings_are_dropped():
    pr = make_pr(files=[make_file("b.py", patch=None, status="renamed", previous_filename="a.py")])
    plan = plan_for([finding(file="b.py", line=1)], ctx=build_context(pr, Settings()))
    assert plan.comments == []
    assert plan.dropped[0].reason == "not_in_diff"


def test_dedupe_keeps_stronger_finding():
    plan = plan_for(
        [
            finding(severity="minor", confidence=0.95, comment="weaker"),
            finding(severity="critical", confidence=0.8, comment="stronger"),
        ]
    )
    assert [c.finding.comment for c in plan.comments] == ["stronger"]
    assert reasons(plan) == [("weaker", "duplicate")]


def test_dedupe_by_normalized_comment_text():
    plan = plan_for(
        [
            finding(line=2, comment="Possible None dereference!"),
            finding(line=3, category="correctness", comment="possible  none dereference"),
        ]
    )
    assert len(plan.comments) == 1
    assert plan.dropped[0].reason == "duplicate"


def test_same_line_different_category_is_not_duplicate():
    plan = plan_for(
        [finding(category="bug", comment="a"), finding(category="security", comment="b")]
    )
    assert len(plan.comments) == 2


def test_cap_keeps_most_severe_then_most_confident():
    lines = [1, 2, 3, 4, 5, 21, 22, 23]
    findings = [
        finding(line=ln, severity=sev, confidence=conf, comment=f"c{ln}")
        for ln, sev, conf in zip(
            lines,
            ["nit", "major", "critical", "minor", "major", "nit", "critical", "minor"],
            [0.9, 0.8, 0.75, 0.9, 0.95, 0.99, 0.9, 0.8],
            strict=True,
        )
    ]
    plan = plan_for(findings, max_comments=3)
    assert [c.finding.comment for c in plan.comments] == ["c22", "c3", "c5"]
    assert sum(1 for d in plan.dropped if d.reason == "over_cap") == 5


@pytest.mark.parametrize("cap", [0, 1, 8])
def test_cap_never_exceeded(cap):
    findings = [finding(line=ln, comment=f"c{ln}") for ln in [1, 2, 3, 4, 5, 21, 22, 23]]
    assert len(plan_for(findings, max_comments=cap).comments) == min(cap, 8)


# --- rendering --------------------------------------------------------------


def test_comment_body_format_and_safe_fence():
    f = finding(comment="Ping @octocat please", suggested_fix="x = '```'\n")
    body = render_comment(f)
    assert body.startswith("**[major] bug** (confidence 0.90)")
    assert "@octocat" not in body  # mention defanged
    assert "````\nx = '```'\n````" in body


def test_summary_has_marker_counts_and_skipped_files():
    pr = make_pr(files=[make_file("src/app.py"), make_file("uv.lock")])
    ctx: ReviewContext = build_context(pr, Settings())
    plan = plan_review(
        ReviewResult(summary="Looks risky.", findings=[finding(), finding(confidence=0.1)]),
        ctx,
        Settings(),
        "m-1",
    )
    assert plan.body.startswith(REVIEW_MARKER)
    assert "Looks risky." in plan.body
    assert "Posted 1 inline comment(s)." in plan.body
    assert "Dropped 1 finding(s): 1 low_confidence." in plan.body
    assert "`uv.lock` (lockfile)" in plan.body
    assert "m-1" in plan.body
