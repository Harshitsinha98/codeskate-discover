"""CodeSkate CLI — the Phase 0 agent pipeline.

    codeskate doctor      check config, keys and model IDs
    codeskate profile     Agent 1 + 2: intake -> evidence-backed skill graph
    codeskate discover    Agent 4: pull live jobs from public ATS boards
    codeskate score       Agent 5: prefilter (free) then LLM score (paid)
    codeskate report      top matches, with reasoning
    codeskate spend       what you have actually spent, per agent
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from . import db
from .agents import discovery, fit_scoring, intake, skill_graph
from .llm import LLM, SpendLimitExceeded
from .models import SkillGraph
from .settings import OUT_DIR, load_settings

app = typer.Typer(add_completion=False, help="A team of AI agents that finds you a better job.")
console = Console()


def _llm_and_db():
    settings = load_settings()
    conn = db.connect()
    return LLM(settings, conn), conn, settings


# --------------------------------------------------------------------------- #


@app.command()
def doctor() -> None:
    """Verify configuration before you spend anything."""
    settings = load_settings()
    conn = db.connect()

    console.print(f"[bold]provider[/bold]      {settings.provider}")
    console.print(f"[bold]cheap model[/bold]   {settings.model_cheap}")
    console.print(f"[bold]smart model[/bold]   {settings.model_smart}")
    console.print(f"[bold]spend limit[/bold]   ${settings.spend_limit_usd:.2f}")
    console.print(f"[bold]spent so far[/bold]  ${db.total_spend(conn):.4f}")
    console.print(f"[bold]api key[/bold]       {'set' if settings.api_key else '[red]MISSING[/red]'}")

    try:
        targets = fit_scoring.load_targets()
        console.print(f"[bold]targets.yaml[/bold]  ok ({len(targets.get('title_include', []))} title filters)")
    except SystemExit as e:
        console.print(f"[red]targets.yaml  {e}[/red]")

    try:
        cfg = discovery.load_company_config()
        console.print(f"[bold]companies[/bold]     {sum(len(v) for v in cfg.values())} boards configured")
    except SystemExit as e:
        console.print(f"[red]companies.yaml  {e}[/red]")

    if not settings.api_key:
        console.print("\n[yellow]Add your API key to .env, then run doctor again to test the models.[/yellow]")
        raise typer.Exit(1)

    llm = LLM(settings, conn)
    for tier in ("cheap", "smart"):
        try:
            console.print(f"[green]ok[/green]  {tier} model responded: {llm.ping(tier)!r}")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]fail[/red]  {tier} model ({settings.model(tier)}): {e}")
            console.print("      Fix the model ID in .env — check your provider's model list.")


@app.command()
def profile(
    show: bool = typer.Option(False, "--show", help="Print the graph instead of rebuilding it"),
) -> None:
    """Agents 1+2 — read data/inbox/ and build an evidence-backed skill graph."""
    conn = db.connect()

    if show:
        existing = db.load_profile(conn)
        if not existing:
            console.print("[yellow]No profile yet. Run `codeskate profile`.[/yellow]")
            raise typer.Exit(1)
        _render_profile(SkillGraph.model_validate(existing))
        return

    console.print("[bold]Agent 1 — Intake[/bold]")
    raw, files = intake.collect()
    console.print(f"  read {len(files)} file(s): {', '.join(files)}  ({len(raw):,} chars)")

    llm, conn, _ = _llm_and_db()
    console.print("\n[bold]Agent 2 — Skill Graph[/bold]")
    with console.status("  extracting..."):
        graph = skill_graph.build(llm, raw)

    db.save_profile(conn, graph.model_dump_json())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "profile.json").write_text(graph.model_dump_json(indent=2))

    _render_profile(graph)
    console.print(f"\nsaved -> data/out/profile.json   spend: ${db.total_spend(conn):.4f}")
    console.print(
        "\n[yellow]Now read it critically.[/yellow] Wrong levels or missing evidence mean the "
        "prompt needs tuning — fix that before scoring anything."
    )


def _render_profile(graph: SkillGraph) -> None:
    console.print(
        f"\n  {graph.candidate_name or 'unknown'} — {graph.seniority or '?'}, "
        f"{graph.total_years_experience} yrs"
    )
    claimable = graph.claimable_skills()
    console.print(
        f"  {len(graph.skills)} skills extracted, "
        f"[green]{len(claimable)} evidence-backed[/green] (usable on a resume), "
        f"{len(graph.achievements)} achievements\n"
    )

    table = Table("skill", "lvl", "yrs", "evidence", box=None, pad_edge=False)
    for s in sorted(graph.skills, key=lambda s: (-s.level, s.name))[:25]:
        mark = "[green]yes[/green]" if s.evidence else "[red]none[/red]"
        table.add_row(s.name, str(s.level), f"{s.years:g}", mark)
    console.print(table)

    unbacked = [s.name for s in graph.skills if not s.evidence]
    if unbacked:
        console.print(
            f"\n[yellow]No evidence ({len(unbacked)}):[/yellow] {', '.join(unbacked[:12])}"
            "\n  These will never be claimed on a tailored resume. Either add proof to your "
            "brag doc, or drop them."
        )


@app.command()
def discover() -> None:
    """Agent 4 — pull live postings from public ATS boards. Free."""
    conn = db.connect()
    console.print("[bold]Agent 4 — Job Discovery[/bold]")
    jobs, errors = discovery.fetch_all()
    jobs = discovery.dedupe(jobs)

    new = db.upsert_jobs(conn, jobs) if jobs else 0
    total = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]

    console.print(f"\n  fetched {len(jobs)} postings, {new} new, {total} in db  [dim](cost: $0)[/dim]")
    if errors:
        console.print(f"\n[yellow]{len(errors)} board(s) unreachable — fix or remove the slug:[/yellow]")
        for e in errors:
            console.print(f"  - {e}")


@app.command()
def score(
    limit: int = typer.Option(40, help="Max jobs to send to the LLM"),
) -> None:
    """Agent 5 — free prefilter, then LLM scoring of the survivors."""
    llm, conn, _ = _llm_and_db()

    graph_raw = db.load_profile(conn)
    if not graph_raw:
        console.print("[red]No profile. Run `codeskate profile` first.[/red]")
        raise typer.Exit(1)

    graph = SkillGraph.model_validate(graph_raw)
    targets = fit_scoring.load_targets()
    brief = skill_graph.profile_brief(graph)
    constraints = fit_scoring.constraints_text(targets)

    candidates = db.unscored_jobs(conn, limit=5000)
    console.print(f"[bold]Agent 5 — Fit Scoring[/bold]\n  {len(candidates)} unscored jobs")

    kept = fit_scoring.prefilter(candidates, targets)
    console.print(
        f"  prefilter (free): kept {len(kept)}, dropped {len(candidates) - len(kept)}"
    )

    kept = kept[:limit]
    if not kept:
        console.print("\n[yellow]Nothing survived the prefilter. Loosen config/targets.yaml.[/yellow]")
        return

    console.print(f"  sending {len(kept)} to {llm.s.model_cheap}\n")
    start_spend = db.total_spend(conn)
    done = 0

    for job in kept:
        try:
            result = fit_scoring.score_job(llm, brief, job, constraints)
        except SpendLimitExceeded as e:
            console.print(f"\n[red]{e}[/red]")
            break
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]![/red] {job['company']}/{job['title'][:40]}: {e}")
            continue

        db.save_fit_score(conn, job["external_id"], result.model_dump())
        done += 1
        colour = {"strong": "green", "stretch": "yellow", "weak": "dim"}[result.verdict]
        console.print(
            f"  [{colour}]{result.score:>3}[/{colour}] {job['company'][:18]:<18} {job['title'][:46]}"
        )

    spent = db.total_spend(conn) - start_spend
    console.print(
        f"\n  scored {done} jobs for ${spent:.4f}"
        + (f" (${spent / done:.5f} each)" if done else "")
    )


@app.command()
def report(
    limit: int = typer.Option(20, help="How many matches to show"),
) -> None:
    """Top matches with reasoning. Also writes data/out/matches.md."""
    conn = db.connect()
    rows = db.top_matches(conn, limit)
    if not rows:
        console.print("[yellow]Nothing scored yet. Run `codeskate score`.[/yellow]")
        raise typer.Exit(1)

    table = Table(title=f"Top {len(rows)} matches")
    table.add_column("#", width=3)
    table.add_column("company", max_width=18)
    table.add_column("title", max_width=40)
    table.add_column("loc", max_width=16)
    for i, r in enumerate(rows, 1):
        colour = {"strong": "green", "stretch": "yellow", "weak": "dim"}[r["verdict"]]
        table.add_row(
            f"[{colour}]{r['score']}[/{colour}]",
            r["company"],
            r["title"],
            (r["location"] or "-"),
        )
    console.print(table)

    lines = ["# Top matches\n"]
    for r in rows:
        lines += [
            f"## {r['score']}/100 — {r['title']} @ {r['company']} ({r['verdict']})",
            f"- Location: {r['location'] or 'n/a'}",
            f"- Apply: {r['url']}",
            f"- Matched: {', '.join(json.loads(r['matched_skills'] or '[]')) or 'none'}",
            f"- Missing: {', '.join(json.loads(r['missing_skills'] or '[]')) or 'none'}",
            f"\n{r['reasoning']}\n",
        ]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "matches.md").write_text("\n".join(lines))

    console.print("\nwritten -> data/out/matches.md")
    console.print(
        "\n[bold yellow]The only test that matters:[/bold yellow] read the top 20 and ask "
        '"would I actually apply to 15 of these?"\n'
        "  No  -> fix targets.yaml and the scoring prompt. Do not build anything else yet.\n"
        "  Yes -> scoring works. Move on to the tailoring agent."
    )


@app.command()
def spend() -> None:
    """Actual API cost, per agent. Your future unit economics live here."""
    conn = db.connect()
    rows = conn.execute(
        """SELECT agent, model, COUNT(*) AS calls,
                  SUM(input_tokens) AS tin, SUM(output_tokens) AS tout,
                  SUM(cache_read) AS cread, SUM(cost_usd) AS cost
           FROM llm_calls GROUP BY agent, model ORDER BY cost DESC"""
    ).fetchall()

    if not rows:
        console.print("No API calls yet — $0.00 spent.")
        return

    table = Table("agent", "model", "calls", "in", "out", "cached", "cost")
    for r in rows:
        table.add_row(
            r["agent"], r["model"], str(r["calls"]),
            f"{r['tin']:,}", f"{r['tout']:,}", f"{r['cread']:,}", f"${r['cost']:.4f}",
        )
    console.print(table)

    settings = load_settings()
    total = db.total_spend(conn)
    console.print(
        f"\ntotal ${total:.4f} of ${settings.spend_limit_usd:.2f} limit "
        f"({total / settings.spend_limit_usd * 100:.1f}%)"
    )


if __name__ == "__main__":
    app()
