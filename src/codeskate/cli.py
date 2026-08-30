"""CodeSkate CLI — 18 agents behind ~20 commands.

Start with `codeskate next`. That is Agent 16, the career manager: it inspects
every piece of state and tells you the one thing to do now. Everything else exists
so it has something to recommend.
"""

from __future__ import annotations

import json
import sqlite3

import typer
from rich.console import Console
from rich.table import Table

from . import db
from .agents import (
    career_manager,
    company_intel,
    compensation,
    discovery,
    fit_scoring,
    gap_analysis,
    interview_prep,
    intake,
    learning_loop,
    mock_interview,
    negotiation,
    outreach,
    pipeline,
    referral,
    resume_tailoring,
    skill_graph,
    submission,
    upskilling,
)
from .llm import LLM, SpendLimitExceeded
from .models import (
    CompanyIntel,
    CompEstimate,
    GapReport,
    OutreachPack,
    PrepBrief,
    SkillGraph,
    TailoredResume,
)
from .settings import OUT_DIR, load_settings

app = typer.Typer(add_completion=False, help="A team of AI agents that finds you a better job.")
console = Console()

VERDICT_COLOUR = {"strong": "green", "stretch": "yellow", "weak": "dim"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _ctx() -> tuple[LLM, sqlite3.Connection]:
    settings = load_settings()
    conn = db.connect()
    return LLM(settings, conn), conn


def _graph(conn: sqlite3.Connection) -> SkillGraph:
    raw = db.load_profile(conn)
    if not raw:
        console.print("[red]No profile yet. Run `codeskate profile` first.[/red]")
        raise typer.Exit(1)
    return SkillGraph.model_validate(raw)


def _resolve(conn: sqlite3.Connection, ident: str) -> sqlite3.Row:
    """Accept a full external_id, a prefix, or a company/title fragment."""
    row = db.job_by_id(conn, ident)
    if row:
        return row

    like = f"%{ident}%"
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE external_id LIKE ? OR company LIKE ? OR title LIKE ?
           LIMIT 12""",
        (f"{ident}%", like, like),
    ).fetchall()

    if not rows:
        console.print(f"[red]No job matching {ident!r}.[/red] Try `codeskate report`.")
        raise typer.Exit(1)
    if len(rows) > 1:
        console.print(f"[yellow]{ident!r} matches {len(rows)} jobs — be more specific:[/yellow]")
        for r in rows:
            console.print(f"  {r['external_id']}  {r['company']} — {r['title'][:50]}")
        raise typer.Exit(1)
    return rows[0]


def _slug(job: sqlite3.Row) -> str:
    """Filesystem-safe form of an external_id (they contain colons)."""
    return str(job["external_id"]).replace(":", "_")


def _write(name: str, content: str) -> str:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return str(path.relative_to(OUT_DIR.parent.parent))


def _spend_note(conn: sqlite3.Connection, before: float) -> None:
    console.print(f"[dim]cost: ${db.total_spend(conn) - before:.4f}[/dim]")


def _constraints() -> str:
    try:
        return fit_scoring.constraints_text(fit_scoring.load_targets())
    except SystemExit:
        return "- none stated"


# --------------------------------------------------------------------------- #
# Agent 16 — the front door
# --------------------------------------------------------------------------- #


@app.command("next")
def next_cmd(
    brief: bool = typer.Option(False, "--brief", help="Add a short LLM daily brief (~$0.001)"),
    limit: int = typer.Option(8, help="Max actions to show"),
) -> None:
    """Agent 16 — what should I do right now? Free unless you pass --brief."""
    conn = db.connect()
    actions = career_manager.next_actions(conn, limit=limit)

    if not actions:
        console.print("[green]Nothing outstanding.[/green] Run `codeskate discover` for fresh jobs.")
        return

    console.print("[bold]Your next actions[/bold]\n")
    for i, action in enumerate(actions, 1):
        console.print(f"[bold cyan]{i}.[/bold cyan] {action.label}")
        console.print(f"   [dim]{action.why}[/dim]")
        console.print(f"   [green]$[/green] {action.command}\n")

    if brief:
        llm, conn = _ctx()
        before = db.total_spend(conn)
        console.print("[bold]Manager's brief[/bold]")
        console.print(career_manager.brief(llm, conn, actions) + "\n")
        _spend_note(conn, before)


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

    for label, loader, unit in (
        ("targets.yaml", fit_scoring.load_targets, "keys"),
        ("companies.yaml", discovery.load_company_config, "boards"),
        ("network.yaml", referral.load_network, "contacts"),
    ):
        try:
            loaded = loader()
        except SystemExit:
            console.print(f"[yellow]{label:<15} missing — some agents will be unavailable[/yellow]")
            continue
        except Exception as e:  # noqa: BLE001 - a malformed config shouldn't kill doctor
            console.print(f"[red]{label:<15} unreadable: {e}[/red]")
            continue

        if label == "companies.yaml":
            size = sum(len(v) for v in loaded.values())
        else:
            size = len(loaded)
        console.print(f"[bold]{label:<15}[/bold] ok ({size} {unit})")

    console.print(f"[bold]{'jobs in db':<15}[/bold] {conn.execute('SELECT COUNT(*) AS c FROM jobs').fetchone()['c']}")
    console.print(f"[bold]{'profile':<15}[/bold] {'built' if db.load_profile(conn) else 'not built'}")

    if not settings.api_key:
        console.print("\n[yellow]Add your API key to .env, then re-run to test the models.[/yellow]")
        raise typer.Exit(1)

    llm = LLM(settings, conn)
    for tier in ("cheap", "smart"):
        try:
            console.print(f"[green]ok[/green]  {tier} model responded: {llm.ping(tier)!r}")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]fail[/red]  {tier} ({settings.model(tier)}): {e}")
            console.print("      Fix the model ID in .env — check your provider's model list.")


# --------------------------------------------------------------------------- #
# Pod 1 — profiling
# --------------------------------------------------------------------------- #


@app.command()
def profile(show: bool = typer.Option(False, "--show", help="Print the stored graph")) -> None:
    """Agents 1+2 — read data/inbox/ and build an evidence-backed skill graph."""
    conn = db.connect()

    if show:
        _render_profile(_graph(conn))
        return

    console.print("[bold]Agent 1 — Intake[/bold]")
    raw, files = intake.collect()
    console.print(f"  read {len(files)} file(s): {', '.join(files)}  ({len(raw):,} chars)")

    llm, conn = _ctx()
    before = db.total_spend(conn)
    console.print("\n[bold]Agent 2 — Skill Graph[/bold]")
    with console.status("  extracting..."):
        graph = skill_graph.build(llm, raw)

    db.save_profile(conn, graph.model_dump_json())
    _write("profile.json", graph.model_dump_json(indent=2))
    _render_profile(graph)
    _spend_note(conn, before)
    console.print(
        "\n[yellow]Read it critically.[/yellow] Wrong levels or missing evidence mean the "
        "prompt needs tuning — fix that before scoring anything."
    )


def _render_profile(graph: SkillGraph) -> None:
    console.print(
        f"\n  {graph.candidate_name or 'unknown'} — {graph.seniority or '?'}, "
        f"{graph.total_years_experience:g} yrs"
    )
    claimable = graph.claimable_skills()
    console.print(
        f"  {len(graph.skills)} skills, [green]{len(claimable)} evidence-backed[/green] "
        f"(usable on a resume), {len(graph.achievements)} achievements\n"
    )
    table = Table("skill", "lvl", "yrs", "evidence", box=None, pad_edge=False)
    for s in sorted(graph.skills, key=lambda s: (-s.level, s.name))[:25]:
        table.add_row(
            s.name, str(s.level), f"{s.years:g}",
            "[green]yes[/green]" if s.evidence else "[red]none[/red]",
        )
    console.print(table)

    unbacked = [s.name for s in graph.skills if not s.evidence]
    if unbacked:
        console.print(
            f"\n[yellow]No evidence ({len(unbacked)}):[/yellow] {', '.join(unbacked[:12])}"
            "\n  These are never claimed on a tailored resume. Add proof, or drop them."
        )


@app.command()
def gaps(
    role: str = typer.Option(..., "--role", help="Target role, e.g. 'Backend Engineer, 3 YOE'"),
) -> None:
    """Agent 3 — what is actually blocking you from this role."""
    llm, conn = _ctx()
    graph = _graph(conn)
    before = db.total_spend(conn)

    console.print(f"[bold]Agent 3 — Gap Analysis[/bold]  target: {role}")
    with console.status("  analysing..."):
        report = gap_analysis.run(llm, graph, role, _constraints())

    db.save_gap_report(conn, role, report.model_dump_json())

    colour = "green" if report.readiness_pct >= 70 else "yellow" if report.readiness_pct >= 45 else "red"
    console.print(f"\n  readiness: [{colour}]{report.readiness_pct}%[/{colour}]")
    console.print(f"  {report.verdict}\n")

    for severity, colour in (("blocking", "red"), ("important", "yellow"), ("nice_to_have", "dim")):
        items = [g for g in report.gaps if g.severity == severity]
        if not items:
            continue
        console.print(f"[{colour}]{severity.replace('_', ' ').upper()}[/{colour}]")
        for g in items:
            console.print(f"  • [bold]{g.skill}[/bold] — {g.why_it_matters}")
            console.print(f"    [dim]fastest proof: {g.fastest_proof}[/dim]")
        console.print()

    if report.strengths_to_lead_with:
        console.print("[green]LEAD WITH[/green]")
        for s in report.strengths_to_lead_with:
            console.print(f"  • {s}")

    _spend_note(conn, before)


# --------------------------------------------------------------------------- #
# Pod 2 — market
# --------------------------------------------------------------------------- #


@app.command()
def discover() -> None:
    """Agent 4 — pull live postings from public ATS boards. Free."""
    import time as _time

    conn = db.connect()
    config = discovery.load_company_config()
    total_boards = sum(len(v) for v in config.values())
    console.print(
        f"[bold]Agent 4 — Job Discovery[/bold]  {total_boards} boards "
        f"[dim](Workday boards are slower — one extra request per posting)[/dim]\n"
    )

    done = 0
    started = _time.monotonic()

    def on_board(ats: str, label: str, count: int, seconds: float) -> None:
        nonlocal done
        done += 1
        status = f"[green]{count:>4}[/green]" if count else "[red]   0[/red]"
        console.print(
            f"  [{done:>2}/{total_boards}] {ats:<11} {label:<22} {status} jobs  "
            f"[dim]{seconds:5.1f}s[/dim]"
        )

    jobs, errors = discovery.fetch_all(config, on_board=on_board)
    jobs = discovery.dedupe(jobs)
    before_india = len(jobs)
    jobs = discovery.india_only(jobs)
    console.print(f"  [dim]India filter: {before_india} -> {len(jobs)} kept[/dim]")

    new = db.upsert_jobs(conn, jobs) if jobs else 0
    total = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    console.print(
        f"\n  fetched {len(jobs)}, {new} new, {total} in db  "
        f"[dim](cost: $0, took {_time.monotonic() - started:.0f}s)[/dim]"
    )

    if errors:
        console.print(f"\n[yellow]{len(errors)} board(s) unreachable — fix or remove the slug:[/yellow]")
        for e in errors:
            console.print(f"  - {e}")


@app.command()
def score(limit: int = typer.Option(40, help="Max jobs to send to the LLM")) -> None:
    """Agent 5 — free prefilter, then LLM scoring of the survivors."""
    llm, conn = _ctx()
    graph = _graph(conn)
    targets = fit_scoring.load_targets()
    brief_text = skill_graph.profile_brief(graph)
    constraints = fit_scoring.constraints_text(targets)

    # Scan every unscored posting: prefiltering is free, and capping before the
    # filter silently hides jobs.
    candidates = db.unscored_candidates(conn)
    console.print(f"[bold]Agent 5 — Fit Scoring[/bold]\n  {len(candidates)} unscored jobs")

    # `report` runs the same filter but also says what each rule threw away, so an
    # empty result explains itself instead of just being empty.
    diag = fit_scoring.report(candidates, targets)
    console.print(f"  prefilter (free): kept {diag['kept']}, dropped {diag['total'] - diag['kept']}")
    for row in diag["rejected"]:
        console.print(f"    [dim]{row['count']:>5}  {row['label'].lower()}[/dim]")

    # Fetch full descriptions only for the postings that survived.
    survivors = fit_scoring.prefilter(candidates, targets)
    kept = db.jobs_by_ids(conn, [row["external_id"] for row in survivors[:limit]])
    if not kept:
        console.print("\n[yellow]Nothing survived the prefilter.[/yellow]")
        for advice in diag["advice"]:
            console.print(f"  [yellow]{advice['problem']}[/yellow]\n  {advice['fix']}")
        return

    console.print(f"  sending {len(kept)} to {llm.s.model_cheap}\n")
    before = db.total_spend(conn)
    done = 0

    for job in kept:
        try:
            result = fit_scoring.score_job(llm, brief_text, job, constraints)
        except SpendLimitExceeded as e:
            console.print(f"\n[red]{e}[/red]")
            break
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]![/red] {job['company']}/{job['title'][:40]}: {e}")
            continue

        db.save_fit_score(conn, job["external_id"], result.model_dump())
        done += 1
        colour = VERDICT_COLOUR[result.verdict]
        console.print(
            f"  [{colour}]{result.score:>3}[/{colour}] {job['company'][:18]:<18} {job['title'][:46]}"
        )

    spent = db.total_spend(conn) - before
    console.print(
        f"\n  scored {done} for ${spent:.4f}" + (f" (${spent / done:.5f} each)" if done else "")
    )


@app.command()
def report(
    limit: int = typer.Option(20, help="How many to show"),
    min_score: int = typer.Option(0, help="Only show at or above this score"),
) -> None:
    """Top matches with reasoning. Also writes data/out/matches.md."""
    conn = db.connect()
    rows = db.top_matches(conn, limit, min_score)
    if not rows:
        console.print("[yellow]Nothing scored yet. Run `codeskate score`.[/yellow]")
        raise typer.Exit(1)

    table = Table(title=f"Top {len(rows)} matches")
    for col, width in (("score", 5), ("company", 16), ("title", 38), ("loc", 14), ("stage", 11), ("id", 22)):
        table.add_column(col, max_width=width)
    for r in rows:
        colour = VERDICT_COLOUR[r["verdict"]]
        table.add_row(
            f"[{colour}]{r['score']}[/{colour}]", r["company"], r["title"],
            r["location"] or "-", r["stage"] or "[dim]—[/dim]", r["external_id"],
        )
    console.print(table)

    lines = ["# Top matches\n"]
    for r in rows:
        lines += [
            f"## {r['score']}/100 — {r['title']} @ {r['company']} ({r['verdict']})",
            f"- Pipeline id: `{r['external_id']}`",
            f"- Location: {r['location'] or 'n/a'}",
            f"- Apply: {r['url']}",
            f"- Matched: {', '.join(json.loads(r['matched_skills'] or '[]')) or 'none'}",
            f"- Missing: {', '.join(json.loads(r['missing_skills'] or '[]')) or 'none'}",
            f"\n{r['reasoning']}\n",
        ]
    written = _write("matches.md", "\n".join(lines))
    console.print(f"\nwritten -> {written}")
    console.print(
        "\n[bold yellow]The only test that matters:[/bold yellow] read the top 20 and ask "
        '"would I actually apply to 15 of these?"\n'
        "  No  -> fix targets.yaml and the scoring prompt. Build nothing else yet.\n"
        "  Yes -> `codeskate shortlist` and move on."
    )


@app.command()
def intel(ident: str = typer.Argument(..., help="Job id, or a company/title fragment")) -> None:
    """Agent 6 — pre-interview company briefing."""
    llm, conn = _ctx()
    job = _resolve(conn, ident)
    before = db.total_spend(conn)

    cached = db.load_company_intel(conn, job["company"])
    if cached:
        console.print(f"[dim]cached briefing for {job['company']}[/dim]\n")
        result = CompanyIntel.model_validate(cached)
    else:
        console.print(f"[bold]Agent 6 — Company Intel[/bold]  {job['company']}")
        with console.status("  briefing..."):
            result = company_intel.run(llm, job["company"], job["title"], job["description"] or "")
        db.save_company_intel(conn, job["company"], result.model_dump_json())

    md = company_intel.to_markdown(result)
    console.print(md)
    written = _write(f"intel/{job['company']}.md", md)
    console.print(f"\nwritten -> {written}")
    if result.confidence == "low":
        console.print(
            "[yellow]Confidence is low.[/yellow] Verify anything time-sensitive "
            "(funding, layoffs, team size) yourself before quoting it in an interview."
        )
    _spend_note(conn, before)


@app.command()
def comp(ident: str = typer.Argument(..., help="Job id or fragment")) -> None:
    """Agent 7 — compensation band, so you don't anchor low."""
    llm, conn = _ctx()
    job = _resolve(conn, ident)
    graph = _graph(conn)
    before = db.total_spend(conn)

    stated = compensation.extract_stated_comp(job["description"] or "")
    if stated:
        console.print(
            f"[green]The posting states a range: {stated[0]:g}-{stated[1]:g} {stated[2]}[/green] "
            "[dim](extracted for free)[/dim]\n"
        )

    console.print(f"[bold]Agent 7 — Compensation[/bold]  {job['title']} @ {job['company']}")
    with console.status("  estimating..."):
        estimate = compensation.run(
            llm, job["title"], job["company"], job["location"], job["description"] or "",
            graph.total_years_experience, graph.seniority, _constraints(),
        )
    db.save_comp_estimate(conn, job["external_id"], estimate.model_dump_json())

    colour = {"high": "green", "medium": "yellow", "low": "red"}[estimate.confidence]
    console.print(f"\n  base   {estimate.base_low:g} – {estimate.base_high:g} {estimate.unit}")
    console.print(f"  total  {estimate.total_low:g} – {estimate.total_high:g} {estimate.unit}")
    console.print(f"  [bold]ask    {estimate.recommended_ask:g} {estimate.unit}[/bold]")
    console.print(f"  confidence: [{colour}]{estimate.confidence}[/{colour}]")
    console.print(f"\n  [dim]{estimate.basis}[/dim]")
    if estimate.confidence == "low":
        console.print(
            "\n[yellow]Treat this as a prior, not market data.[/yellow] Cross-check against "
            "Levels.fyi or AmbitionBox before quoting a number."
        )
    _spend_note(conn, before)


# --------------------------------------------------------------------------- #
# Pod 3 — execution
# --------------------------------------------------------------------------- #


@app.command()
def shortlist(
    min_score: int = typer.Option(70, help="Minimum fit score to pursue"),
    limit: int = typer.Option(10, help="Max to add"),
) -> None:
    """Move strong matches into the pipeline. Free."""
    conn = db.connect()
    rows = conn.execute(
        """SELECT f.external_id, f.score, j.company, j.title
           FROM fit_scores f JOIN jobs j ON j.external_id = f.external_id
           LEFT JOIN applications a ON a.external_id = f.external_id
           WHERE f.score >= ? AND a.external_id IS NULL
           ORDER BY f.score DESC LIMIT ?""",
        (min_score, limit),
    ).fetchall()

    if not rows:
        console.print(f"[yellow]No unpursued jobs at score >= {min_score}.[/yellow]")
        return

    for r in rows:
        db.add_application(conn, r["external_id"])
        db.record_outcome(conn, "shortlisted", f"score {r['score']}", r["external_id"])
        console.print(f"  [green]+[/green] {r['score']:>3}  {r['company'][:18]:<18} {r['title'][:46]}")

    console.print(f"\n  {len(rows)} added to pipeline. Next: [green]codeskate next[/green]")


@app.command()
def tailor(ident: str = typer.Argument(..., help="Job id or fragment")) -> None:
    """Agent 8 — rewrite your resume for this job, then verify nothing was invented."""
    llm, conn = _ctx()
    job = _resolve(conn, ident)
    graph = _graph(conn)
    db.add_application(conn, job["external_id"])
    before = db.total_spend(conn)

    console.print(f"[bold]Agent 8 — Resume Tailoring[/bold]  {job['title']} @ {job['company']}")
    if not graph.achievements:
        console.print(
            "[red]Your profile has no achievements.[/red] This agent can only rearrange facts "
            "you gave it. Fill in data/inbox/brag.md (see docs/brag-template.md) and re-run "
            "`codeskate profile`."
        )
        raise typer.Exit(1)

    with console.status("  writing..."):
        resume = resume_tailoring.run(
            llm, graph, job["title"], job["company"], job["description"] or ""
        )

    violations = resume_tailoring.verify(resume, graph)
    md = resume_tailoring.to_markdown(resume, job["title"], job["company"])
    db.save_artifact(conn, "resume", resume.model_dump_json(), job["external_id"])
    # Re-tailoring an application that has already moved on is fine — just don't
    # drag it backwards through the state machine.
    if pipeline.can_move(db.get_application(conn, job["external_id"])["stage"], "tailored"):
        pipeline.advance(conn, job["external_id"], "tailored", "resume generated")

    console.print(f"\n[bold]{resume.headline}[/bold]\n{resume.summary}\n")
    for b in resume.bullets:
        console.print(f"  • {b.text}")
        console.print(f"    [dim]from: {b.source_achievement}[/dim]")
    console.print(f"\n  skills: {', '.join(resume.skills_line)}")
    if resume.omitted_and_why:
        console.print("\n  [dim]omitted:[/dim]")
        for o in resume.omitted_and_why:
            console.print(f"    [dim]- {o}[/dim]")

    if violations:
        console.print(f"\n[red]FABRICATION CHECK FAILED — {len(violations)} issue(s)[/red]")
        for v in violations:
            console.print(f"  [red]![/red] {v}")
        console.print(
            "\n[red]Do not send this as-is.[/red] Either the model invented something, or your "
            "brag document is missing the achievement it drew on. Fix and re-run."
        )
    else:
        console.print("\n[green]Fabrication check passed[/green] — every bullet traces to a real achievement.")

    console.print(f"\nwritten -> {_write(f'resumes/{_slug(job)}.md', md)}")
    _spend_note(conn, before)


@app.command("outreach")
def outreach_cmd(ident: str = typer.Argument(..., help="Job id or fragment")) -> None:
    """Agent 9 — cover letter, recruiter DM, hiring-manager email, follow-up."""
    llm, conn = _ctx()
    job = _resolve(conn, ident)
    graph = _graph(conn)

    stored = db.latest_artifact(conn, "resume", job["external_id"])
    if not stored:
        console.print(f"[yellow]Tailor first:[/yellow] codeskate tailor {job['external_id']}")
        raise typer.Exit(1)

    before = db.total_spend(conn)
    console.print(f"[bold]Agent 9 — Outreach[/bold]  {job['company']}")
    with console.status("  drafting..."):
        pack = outreach.run(
            llm, graph, TailoredResume.model_validate(stored),
            job["title"], job["company"], job["description"] or "",
        )

    db.save_artifact(conn, "outreach", pack.model_dump_json(), job["external_id"])
    md = outreach.to_markdown(pack, job["company"])
    console.print(md)
    console.print(f"\nwritten -> {_write(f'outreach/{_slug(job)}.md', md)}")
    _spend_note(conn, before)


@app.command()
def refer(ident: str = typer.Argument(..., help="Job id or fragment")) -> None:
    """Agent 10 — find contacts for this company and draft the ask."""
    llm, conn = _ctx()
    job = _resolve(conn, ident)
    graph = _graph(conn)

    contacts = referral.find_contacts(job["company"])
    if not contacts:
        console.print(
            f"[yellow]No contacts for {job['company']} in config/network.yaml.[/yellow]\n"
            "This agent never scrapes LinkedIn — add people you actually know, including "
            "`can_intro_at` for companies they could introduce you to."
        )
        raise typer.Exit(1)

    stored = db.latest_artifact(conn, "resume", job["external_id"])
    tailored = TailoredResume.model_validate(stored) if stored else None
    if tailored and tailored.bullets:
        top_proof = tailored.bullets[0].text
    elif graph.achievements:
        top_proof = graph.achievements[0].headline
    else:
        top_proof = "n/a"

    before = db.total_spend(conn)
    console.print(f"[bold]Agent 10 — Referral[/bold]  {len(contacts)} contact(s) for {job['company']}\n")

    notes = []
    for contact in contacts:
        request = referral.run(
            llm, graph, contact, job["title"], job["company"], job["url"], top_proof
        )
        route = "works there" if contact["_route"] == "direct" else "can introduce you"
        console.print(
            f"[bold cyan]{request.contact_name}[/bold cyan] "
            f"[dim]({contact.get('strength', 'cold')}, {route})[/dim]"
        )
        console.print(f"  [dim]angle: {request.angle}[/dim]\n")
        console.print("  " + request.message.replace("\n", "\n  ") + "\n")
        if request.what_to_attach:
            console.print(f"  [dim]attach: {', '.join(request.what_to_attach)}[/dim]\n")
        notes.append(f"Message {request.contact_name} ({route})")

    db.save_artifact(conn, "referral", json.dumps(notes), job["external_id"])
    console.print(
        "[yellow]Send these before applying cold.[/yellow] A referred application gets read; "
        "a cold one gets filtered. Give it 2 days."
    )
    _spend_note(conn, before)


@app.command()
def submit(ident: str = typer.Argument(..., help="Job id or fragment")) -> None:
    """Agent 11 — assemble a review-ready application packet. You click submit."""
    llm, conn = _ctx()
    job = _resolve(conn, ident)
    graph = _graph(conn)

    stored_resume = db.latest_artifact(conn, "resume", job["external_id"])
    if not stored_resume:
        console.print(f"[yellow]Tailor first:[/yellow] codeskate tailor {job['external_id']}")
        raise typer.Exit(1)

    resume = TailoredResume.model_validate(stored_resume)
    stored_outreach = db.latest_artifact(conn, "outreach", job["external_id"])
    pack = OutreachPack.model_validate(stored_outreach) if stored_outreach else None
    referral_notes = db.latest_artifact(conn, "referral", job["external_id"])

    before = db.total_spend(conn)
    console.print(f"[bold]Agent 11 — Submission[/bold]  {job['title']} @ {job['company']}")
    with console.status("  drafting screening answers..."):
        answers = submission.draft_answers(
            llm, graph, resume, job["title"], job["company"],
            job["description"] or "", _constraints(),
        )

    folder = submission.build_packet(
        conn, job["external_id"], job["company"], job["title"], job["url"],
        resume_tailoring.to_markdown(resume, job["title"], job["company"]),
        pack, answers,
        referral_notes if isinstance(referral_notes, list) else None,
    )
    db.save_artifact(conn, "packet", json.dumps({"folder": folder}), job["external_id"])

    console.print(f"\n  packet -> [bold]{folder}[/bold]")
    console.print("    resume.md  outreach.md  answers.md  checklist.md")
    console.print(f"\n  apply at: {job['url']}")
    console.print(
        "\n[yellow]This agent stops here deliberately.[/yellow] Auto-submitting needs your "
        "logged-in session on each board, which is exactly what their terms forbid — and one "
        "mangled auto-application costs you that company for a year.\n"
        f"\nAfter you submit: [green]codeskate stage {job['external_id']} applied[/green]"
    )
    _spend_note(conn, before)


# --------------------------------------------------------------------------- #
# Pod 4 — interview and close
# --------------------------------------------------------------------------- #


@app.command()
def prep(
    ident: str = typer.Argument(..., help="Job id or fragment"),
    stage: str = typer.Option("screen", help="screen | interview | onsite"),
) -> None:
    """Agent 12 — questions, STAR stories, and where you'll get caught out."""
    llm, conn = _ctx()
    job = _resolve(conn, ident)
    graph = _graph(conn)
    before = db.total_spend(conn)

    cached_intel = db.load_company_intel(conn, job["company"])
    intel_obj = CompanyIntel.model_validate(cached_intel) if cached_intel else None

    console.print(f"[bold]Agent 12 — Interview Prep[/bold]  {stage} at {job['company']}")
    with console.status("  building brief..."):
        brief_obj = interview_prep.run(
            llm, graph, job["title"], job["company"], job["description"] or "", intel_obj, stage
        )

    db.save_artifact(conn, "prep_brief", brief_obj.model_dump_json(), job["external_id"])
    md = interview_prep.to_markdown(brief_obj, job["title"], job["company"])

    console.print("\n[bold]This loop will be decided on[/bold]")
    for f in brief_obj.role_focus:
        console.print(f"  • {f}")
    console.print("\n[bold red]Where you will get caught out[/bold red]")
    for w in brief_obj.weak_spots:
        console.print(f"  • {w}")
    console.print(
        f"\n  {len(brief_obj.likely_questions)} questions and "
        f"{len(brief_obj.star_stories)} STAR stories in the file."
    )
    console.print(f"\nwritten -> {_write(f'prep/{_slug(job)}.md', md)}")
    console.print(f"\nNow rehearse: [green]codeskate mock {job['external_id']}[/green]")
    _spend_note(conn, before)


@app.command()
def mock(
    ident: str = typer.Argument(..., help="Job id or fragment"),
    count: int = typer.Option(5, help="How many questions"),
    stage: str = typer.Option("screen", help="screen | interview | onsite"),
) -> None:
    """Agent 13 — multi-turn mock interview with honest scoring."""
    llm, conn = _ctx()
    job = _resolve(conn, ident)
    graph = _graph(conn)
    before = db.total_spend(conn)

    stored_brief = db.latest_artifact(conn, "prep_brief", job["external_id"])
    brief_obj = PrepBrief.model_validate(stored_brief) if stored_brief else None

    console.print(f"[bold]Agent 13 — Mock Interview[/bold]  {stage} at {job['company']}")
    console.print("[dim]Type your answer and press Enter. Blank line to skip a question.[/dim]\n")

    with console.status("  preparing questions..."):
        questions = mock_interview.generate_questions(
            llm, job["title"], job["company"], job["description"] or "", stage, count, brief_obj
        )

    turns: list[dict] = []
    for i, q in enumerate(questions, 1):
        console.print(f"[bold cyan]Q{i}/{len(questions)}[/bold cyan] [dim][{q.kind}][/dim]")
        console.print(f"  {q.question}\n")
        try:
            answer = typer.prompt("  your answer", default="", show_default=False)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Session ended early.[/yellow]")
            break

        with console.status("  scoring..."):
            fb = mock_interview.evaluate(llm, graph, q, answer, job["title"], job["company"])

        colour = {"pass": "green", "borderline": "yellow", "fail": "red"}[fb.verdict]
        console.print(f"\n  [{colour}]{fb.score}/100 — {fb.verdict}[/{colour}]")
        for p in fb.problems:
            console.print(f"  [red]-[/red] {p}")
        for m in fb.missing_from_answer:
            console.print(f"  [yellow]![/yellow] you had this and didn't use it: {m}")
        console.print(f"\n  [dim]stronger version:[/dim]\n  {fb.model_answer}\n")

        turns.append(
            {"question": q.question, "kind": q.kind, "answer": answer, "feedback": fb.model_dump()}
        )

    if not turns:
        console.print("[yellow]No answers recorded.[/yellow]")
        return

    avg = sum(t["feedback"]["score"] for t in turns) / len(turns)
    db.save_mock_session(conn, stage, json.dumps(turns), avg, job["external_id"])
    md = mock_interview.session_markdown(turns, job["title"], job["company"], avg)

    console.print(f"[bold]Session average: {avg:.0f}/100[/bold] over {len(turns)} question(s)")
    console.print(f"written -> {_write(f'mocks/{_slug(job)}.md', md)}")
    _spend_note(conn, before)


@app.command()
def negotiate(
    ident: str = typer.Argument(..., help="Job id or fragment"),
    offer: str = typer.Option(..., "--offer", help="The offer as stated, in your own words"),
) -> None:
    """Agent 14 — offer assessment and a counter script you can read out."""
    llm, conn = _ctx()
    job = _resolve(conn, ident)
    graph = _graph(conn)
    before = db.total_spend(conn)

    stored = db.load_comp_estimate(conn, job["external_id"])
    estimate = CompEstimate.model_validate(stored) if stored else None
    if not estimate:
        console.print(
            f"[dim]No comp band yet — run `codeskate comp {job['external_id']}` first for a "
            "stronger plan. Continuing without it.[/dim]\n"
        )

    console.print(f"[bold]Agent 14 — Negotiation[/bold]  {job['company']}")
    with console.status("  building plan..."):
        plan = negotiation.run(
            llm, graph, job["company"], job["title"], offer, estimate, _constraints()
        )

    db.save_artifact(conn, "negotiation", plan.model_dump_json(), job["external_id"])
    md = negotiation.to_markdown(plan, job["company"])
    console.print("\n" + md)
    console.print(f"\nwritten -> {_write(f'negotiation/{_slug(job)}.md', md)}")
    _spend_note(conn, before)


@app.command()
def upskill(
    weeks: int = typer.Option(6, help="Plan length"),
    hours: int = typer.Option(8, help="Hours available per week"),
) -> None:
    """Agent 15 — turn your gaps into a plan that produces real artefacts."""
    llm, conn = _ctx()
    graph = _graph(conn)

    stored = db.latest_gap_report(conn)
    if not stored:
        console.print("[yellow]Run `codeskate gaps --role '<target>'` first.[/yellow]")
        raise typer.Exit(1)

    before = db.total_spend(conn)
    console.print(f"[bold]Agent 15 — Upskilling[/bold]  {weeks} weeks at {hours}h/week")
    with console.status("  planning..."):
        plan = upskilling.run(llm, graph, GapReport.model_validate(stored), weeks, hours)

    db.save_artifact(conn, "upskill_plan", plan.model_dump_json())
    md = upskilling.to_markdown(plan)
    console.print("\n" + md)
    console.print(f"\nwritten -> {_write('upskill-plan.md', md)}")
    console.print(
        "\n[dim]When you finish a milestone, add the proof to data/inbox/brag.md and re-run "
        "`codeskate profile` — the skill becomes claimable on tailored resumes.[/dim]"
    )
    _spend_note(conn, before)


# --------------------------------------------------------------------------- #
# Pod 5 — pipeline and learning
# --------------------------------------------------------------------------- #


@app.command()
def stage(
    ident: str = typer.Argument(..., help="Job id or fragment"),
    to: str = typer.Argument(..., help=f"One of: {', '.join(pipeline.STAGES)}"),
    note: str = typer.Option("", help="Optional note"),
) -> None:
    """Agent 17 — move an application forward. Illegal jumps are refused."""
    conn = db.connect()
    job = _resolve(conn, ident)
    try:
        pipeline.advance(conn, job["external_id"], to, note)
    except pipeline.InvalidTransition as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"[green]{job['company']} — {job['title'][:40]} -> {to}[/green]")


@app.command(name="pipeline")
def pipeline_cmd() -> None:
    """Agent 17 — your funnel, stale applications, and likely ghosts. Free."""
    conn = db.connect()
    funnel = pipeline.funnel(conn)

    if not any(funnel.values()):
        console.print("[yellow]Pipeline is empty.[/yellow] Run `codeskate shortlist`.")
        return

    table = Table("stage", "count", box=None)
    for stage_name, count in funnel.items():
        if count:
            table.add_row(stage_name, str(count))
    console.print(table)

    rates = pipeline.conversion_rates(conn)
    if rates["callback_rate_pct"] is not None:
        console.print(f"\n  callback rate: [bold]{rates['callback_rate_pct']}%[/bold]")
    else:
        console.print(
            f"\n  [dim]{rates['applied_total']:.0f} applications — need 10+ before a rate "
            "means anything[/dim]"
        )

    stale = pipeline.needs_followup(conn)
    if stale:
        console.print(f"\n[yellow]Needs a follow-up ({len(stale)}):[/yellow]")
        for r in stale:
            console.print(f"  {r['days_in_stage']:>3}d  {r['company'][:18]:<18} {r['stage']}")

    ghosts = pipeline.ghost_candidates(conn)
    if ghosts:
        console.print(f"\n[dim]Probably ghosted ({len(ghosts)}) — close these out:[/dim]")
        for r in ghosts:
            console.print(f"  {r['days_in_stage']:>3}d  {r['company'][:18]:<18} {r['stage']}")


@app.command()
def followup(ident: str = typer.Argument(..., help="Job id or fragment")) -> None:
    """Show the follow-up message already drafted for this application. Free."""
    conn = db.connect()
    job = _resolve(conn, ident)
    stored = db.latest_artifact(conn, "outreach", job["external_id"])
    if not stored:
        console.print(f"[yellow]No outreach drafted yet:[/yellow] codeskate outreach-cmd {job['external_id']}")
        raise typer.Exit(1)
    pack = OutreachPack.model_validate(stored)
    console.print(f"[bold]Follow-up for {job['company']}[/bold]\n")
    console.print(pack.followup_message)


@app.command()
def learn() -> None:
    """Agent 18 — is any of this actually working? Stats are free; interpretation costs."""
    # Deliberately does not construct the LLM client yet: the statistics are free
    # and should be viewable without an API key configured.
    conn = db.connect()
    stats = learning_loop.compute_stats(conn)

    console.print("[bold]Agent 18 — Learning Loop[/bold]  [dim](stats computed in SQL, $0)[/dim]\n")
    console.print(f"  applications: {stats['applications_sent']}   responses: {stats['responses']}")
    if stats["overall_response_rate_pct"] is not None:
        console.print(f"  response rate: [bold]{stats['overall_response_rate_pct']}%[/bold]")

    if stats["by_fit_score"]:
        console.print("\n  [bold]Does fit score predict callbacks?[/bold]")
        table = Table("score band", "applied", "responses", "rate", box=None)
        for row in stats["by_fit_score"]:
            table.add_row(
                row["bucket"], str(row["applications"]), str(row["responses"]),
                f"{row['response_rate_pct']}%" if row["response_rate_pct"] is not None else "-",
            )
        console.print(table)

    if stats["by_route"]:
        console.print("\n  [bold]Referred vs cold[/bold]")
        for row in stats["by_route"]:
            rate = f"{row['response_rate_pct']}%" if row["response_rate_pct"] is not None else "-"
            console.print(f"    {row['route']:<9} {row['applications']:>3} applied  {rate:>6}")

    if not stats["sample_is_meaningful"]:
        console.print(
            f"\n[yellow]Only {stats['applications_sent']} applications recorded.[/yellow] "
            f"Below {learning_loop.MIN_SAMPLE} there is nothing to conclude — any pattern here "
            "is noise. Skipping the LLM interpretation to save you money."
        )
        return

    llm, conn = _ctx()
    before = db.total_spend(conn)
    with console.status("  interpreting..."):
        insight = learning_loop.run(llm, stats)

    for title, items, colour in (
        ("What worked", insight.what_worked, "green"),
        ("What failed", insight.what_failed, "red"),
        ("Change this next", insight.recommended_changes, "cyan"),
    ):
        if items:
            console.print(f"\n[{colour}]{title}[/{colour}]")
            for i in items:
                console.print(f"  • {i}")

    console.print(f"\n[yellow]Sample-size caveat:[/yellow] {insight.confidence_warning}")
    _spend_note(conn, before)


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
            r["agent"], r["model"], str(r["calls"]), f"{r['tin']:,}",
            f"{r['tout']:,}", f"{r['cread']:,}", f"${r['cost']:.4f}",
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
