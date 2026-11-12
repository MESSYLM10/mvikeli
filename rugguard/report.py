"""Evaluation reports: a CSV with one traceable row per token, and a Markdown summary.

Every row carries the IDs needed to retrace it: the outcome and score rule versions, when each was
computed, the snapshot the score came from, the launch transaction, and the evidence lines.
"""

from __future__ import annotations

import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .analysis.outcome import OutcomeRules
from .analysis.score import ScoreRules, auc
from .scoring import EVAL_LABELS

COMPONENTS = list(ScoreRules().weights)

CSV_FIELDS = (
    ["run_id", "token_set", "token_id", "verdict", "outcome", "label_source", "human_reason",
     "score", "band", "threshold", "coverage", "weighted_score", "floors_applied"]
    + [f"sub_{c}" for c in COMPONENTS]
    + ["launch_tx", "age_days", "pool_id", "pool_near_now", "near_kept_fraction", "price_ratio",
       "insider_exit", "redeploys_after_launch",
       "outcome_rules", "outcome_computed_at", "outcome_id",
       "score_rules", "score_computed_at", "score_id", "snapshot_id",
       "outcome_evidence", "score_reasons"]
)


def verdict(label: str, score: float | None, threshold: float) -> str:
    """caught / missed / false_alarm / correct_clear, or excluded when the token cannot be judged."""
    if label not in EVAL_LABELS or score is None:
        return "excluded"
    flagged = score >= threshold
    if label == "rugged":
        return "caught" if flagged else "missed"
    return "false_alarm" if flagged else "correct_clear"


def exclusion_reason(label: str, score: float | None) -> str:
    if score is None and label in EVAL_LABELS:
        return "not scored"
    return {"needs_review": "outcome needs review", "pending": "too young to judge",
            "spoiled": "never reached a pool"}.get(label, "")


def _loads(text: str | None, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _round(x, n=4):
    return "" if x is None else round(x, n)


def detail_rows(db, ev: dict, token_set: str, run_id: str) -> list[dict]:
    """Expand evaluate()'s rows with the stored outcome and score detail behind each one."""
    out = []
    for r in sorted(ev["rows"], key=lambda r: (r["score"] is None, -(r["score"] or 0), r["token_id"])):
        token = r["token_id"]
        o = db.query("SELECT outcome_id, rules_version, evidence_json, facts_json, computed_at FROM outcomes "
                     "WHERE token_id = ? ORDER BY outcome_id DESC LIMIT 1", (token,))
        s = db.query("""SELECT rs.score_id, rs.snapshot_id, rs.reasons_json, rs.facts_json, rs.weights_version,
                               rs.computed_at FROM risk_scores rs JOIN snapshots sn ON sn.snapshot_id = rs.snapshot_id
                        WHERE sn.token_id = ? AND rs.kind = 'total' ORDER BY rs.score_id DESC LIMIT 1""", (token,))
        of = _loads(o[0]["facts_json"], {}) if o else {}
        sf = _loads(s[0]["facts_json"], {}) if s else {}
        comps = sf.get("components", {})
        age = of.get("age_hours")
        row = {
            "run_id": run_id, "token_set": token_set, "token_id": token,
            "verdict": verdict(r["label"], r["score"], ev["threshold"]),
            "outcome": r["label"], "label_source": r["label_source"], "human_reason": r.get("human_reason") or "",
            "score": _round(r["score"], 1), "band": r["band"] or "", "threshold": ev["threshold"],
            "coverage": _round(sf.get("coverage"), 2), "weighted_score": _round(sf.get("weighted_score"), 1),
            "floors_applied": " | ".join(sf.get("floors_applied", [])),
            "launch_tx": of.get("launch_tx", ""), "age_days": _round(age / 24 if age is not None else None, 1),
            "pool_id": of.get("pool_id", ""), "pool_near_now": _round(of.get("pool_near")),
            "near_kept_fraction": _round(of.get("near_kept_fraction")), "price_ratio": _round(of.get("price_ratio"), 6),
            "insider_exit": of.get("insider_exit", ""), "redeploys_after_launch": of.get("redeploys_after_launch", ""),
            "outcome_rules": o[0]["rules_version"] if o else "", "outcome_computed_at": o[0]["computed_at"] if o else "",
            "outcome_id": o[0]["outcome_id"] if o else "",
            "score_rules": s[0]["weights_version"] if s else "", "score_computed_at": s[0]["computed_at"] if s else "",
            "score_id": s[0]["score_id"] if s else "", "snapshot_id": s[0]["snapshot_id"] if s else "",
            "outcome_evidence": " | ".join(_loads(o[0]["evidence_json"], []) if o else []),
            "score_reasons": " | ".join(_loads(s[0]["reasons_json"], []) if s else []),
        }
        for c in COMPONENTS:
            row[f"sub_{c}"] = _round((comps.get(c) or {}).get("subscore"), 3)
        row["_exclusion"] = exclusion_reason(r["label"], r["score"])
        out.append(row)
    return out


BASELINE_NAMES = {
    "upgrade": "code can be replaced", "owner_powers": "owner powers (mint, pause, blacklist)",
    "concentration": "top wallets hold most of the supply", "deployer": "deployer wallet holds most of the supply",
    "lp_control": "a wallet added the liquidity", "liquidity": "thin pool at launch",
    "early_buyers": "first-hour buying is concentrated or linked",
}


def baselines(rows: list[dict]) -> list[dict]:
    """Simpler rules on the same tokens, so the combined score can be compared with each signal alone.

    "Flag everything" shows what the base rate alone buys; each single signal flags a token when its
    subscore is at least 0.5 and ranks tokens by that subscore (a missing signal counts as 0).
    """
    judged = [r for r in rows if r["verdict"] != "excluded"]

    def measure(name: str, value) -> dict:
        rug = [value(r) for r in judged if r["outcome"] == "rugged"]
        other = [value(r) for r in judged if r["outcome"] != "rugged"]
        return {"rule": name, "caught": sum(v >= 0.5 for v in rug), "rugged": len(rug),
                "false_alarms": sum(v >= 0.5 for v in other), "others": len(other), "auc": auc(rug, other)}

    out = [measure("Flag every token", lambda r: 1.0)]
    for c in COMPONENTS:
        out.append(measure(f"Only: {BASELINE_NAMES.get(c, c)}",
                           lambda r, c=c: r[f"sub_{c}"] if r[f"sub_{c}"] != "" else 0.0))
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def git_commit(cwd: Path | None = None) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _md(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _pct(x) -> str:
    return "–" if x is None else f"{x:.0%}"


def markdown(ev: dict, rows: list[dict], *, token_set: str, run_id: str, command: str, csv_name: str,
             commit: str) -> str:
    th = ev["threshold"]
    judged = [r for r in rows if r["verdict"] != "excluded"]
    count = {v: sum(r["verdict"] == v for r in rows) for v in ("caught", "missed", "false_alarm", "correct_clear")}
    versions_o = sorted({r["outcome_rules"] for r in rows if r["outcome_rules"]})
    versions_s = sorted({r["score_rules"] for r in rows if r["score_rules"]})
    L = [f"# Mvikeli evaluation: {token_set}", "",
         f"- Run: `{run_id}` (UTC), code commit `{commit}`",
         f"- Command: `{command}`",
         f"- Outcome rules: {', '.join(versions_o) or '–'}; score rules: {', '.join(versions_s) or '–'}",
         f"- Per-token data: `{csv_name}` (one row per token, with rule versions, snapshot IDs and evidence)", "",
         "## Result", ""]
    if ev["rugged"] and ev["judged"] > ev["rugged"]:
        L += ["| Measure | Value |", "|---|---|",
              f"| Tokens judged | {ev['judged']} ({ev['excluded']} excluded) |",
              f"| Rugs caught at score ≥ {th:.0f} | {ev['caught']} of {ev['rugged']} (recall {_pct(ev['recall'])}) |",
              f"| False alarms | {ev['false_alarms']} of {ev['judged'] - ev['rugged']} non-rugged |",
              f"| Precision | {_pct(ev['precision'])} |",
              f"| AUC | {ev['auc']:.2f} (0.5 = coin flip, 1.0 = perfect ranking) |",
              "| Mean score | " + ", ".join(f"{k} {v}" for k, v in ev["mean_score"].items()) + " |", "",
              f"| | Scored ≥ {th:.0f} | Scored < {th:.0f} |", "|---|---|---|",
              f"| Rugged | {count['caught']} caught | {count['missed']} missed |",
              f"| Not rugged | {count['false_alarm']} false alarm(s) | {count['correct_clear']} correctly clear |", ""]
    else:
        L += ["Not enough judged tokens: need at least one rugged and one non-rugged token with a score.", ""]

    notes = []
    if ev["judged"] < 30:
        notes.append(f"Small sample ({ev['judged']} judged tokens): treat these rates as indicative, not established.")
    scores = [r["score"] for r in judged]
    if scores:
        top = max(set(scores), key=scores.count)
        if scores.count(top) >= max(3, len(scores) // 2):
            notes.append(f"{scores.count(top)} of {len(scores)} judged tokens share the score {top}: the red-flag "
                         "floor decides most cases, so the score acts close to a yes/no rule.")
    if ev["rugged"] and ev["judged"] > ev["rugged"]:
        L += ["## Compared with simpler rules", "",
              "Each simpler rule runs on the same judged tokens. A single signal flags a token when its subscore is "
              "at least 0.5.", "",
              "| Rule | Rugs caught | False alarms | AUC |", "|---|---|---|---|",
              f"| **Mvikeli score ≥ {th:.0f}** | **{ev['caught']} of {ev['rugged']}** | "
              f"**{ev['false_alarms']} of {ev['judged'] - ev['rugged']}** | **{ev['auc']:.2f}** |"]
        for b in baselines(rows):
            a = "–" if b["auc"] is None else f"{b['auc']:.2f}"
            L.append(f"| {b['rule']} | {b['caught']} of {b['rugged']} | {b['false_alarms']} of {b['others']} | {a} |")
        L.append("")
    if notes:
        L += ["## Read with care", ""] + [f"- {n}" for n in notes] + [""]

    L += ["## Tokens", "", "| Token | Outcome | Score | Band | Verdict |", "|---|---|---|---|---|"]
    for r in rows:
        lbl = r["outcome"] + (" (human)" if r["label_source"] == "human" else "")
        sc = "–" if r["score"] == "" else f"{r['score']:.0f}"
        v = r["verdict"].replace("_", " ") + (f" ({r['_exclusion']})" if r["verdict"] == "excluded" else "")
        L.append(f"| `{_md(r['token_id'])}` | {lbl} | {sc} | {r['band'] or '–'} | {v} |")
    L.append("")

    errors = [r for r in rows if r["verdict"] in ("missed", "false_alarm")]
    if errors:
        L += ["## Misses and false alarms", ""]
        for r in errors:
            L += [f"**`{_md(r['token_id'])}`**: {r['verdict'].replace('_', ' ')}, outcome {r['outcome']}, "
                  f"score {r['score']:.0f}", ""]
            L += [f"- Outcome: {_md(e)}" for e in r["outcome_evidence"].split(" | ") if e][:4]
            L += [f"- Score: {_md(e)}" for e in r["score_reasons"].split(" | ") if e][:3]
            L.append("")

    L += ["## Rules used", "", "```json",
          json.dumps({"score": ScoreRules().as_dict(), "outcome": OutcomeRules().as_dict()}, indent=2),
          "```", "",
          "The rule versions above identify the thresholds; the JSON shows the defaults for this code commit.", "",
          "Data: NEAR RPC, FastNEAR, Rhea (Ref Finance) pools. Powered by Nearblocks.io APIs.", ""]
    return "\n".join(L)


def run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_report(db, ev: dict, out_stem: Path, token_set: str, command: str,
                 run_id: str | None = None) -> tuple[Path, Path]:
    run_id = run_id or run_stamp()
    rows = detail_rows(db, ev, token_set, run_id)
    csv_path, md_path = out_stem.with_suffix(".csv"), out_stem.with_suffix(".md")
    write_csv(csv_path, rows)
    md_path.write_text(markdown(ev, rows, token_set=token_set, run_id=run_id, command=command,
                                csv_name=csv_path.name, commit=git_commit(Path(__file__).parent)),
                       encoding="utf-8")
    return csv_path, md_path
