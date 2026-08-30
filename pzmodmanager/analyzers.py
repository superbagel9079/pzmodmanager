"""The detection rules.

Each rule is a function taking the analysis context and returning findings. No
rule ever uses the word "incompatible" on its own authority: the engine has no
such notion. They describe overlaps and, when the load order is known, say which
mod wins.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from .assets import classify
from .loadorder import LoadOrder
from .models import Finding, Mod, Severity
from .scripts import SIGNIFICANT_KINDS
from .selection import probable_typo, resolve_requirement
from .steam import stated_incompatibilities

log = logging.getLogger(__name__)

# Severity of a file collision according to what kind of file it is.
COLLISION_SEVERITY = {
    "lua_client": Severity.HIGH,
    "lua_shared": Severity.HIGH,
    "lua_server": Severity.MEDIUM,
    "lua_other": Severity.MEDIUM,
    "script": Severity.MEDIUM,
    "map": Severity.HIGH,
    "model": Severity.LOW,
    "sound": Severity.LOW,
    "texture": Severity.LOW,
    "translation": Severity.LOW,
    "other": Severity.LOW,
}

CATEGORY_LABEL = {
    "lua_client": "client Lua script",
    "lua_shared": "shared Lua script",
    "lua_server": "server Lua script",
    "lua_other": "Lua script",
    "script": "object script file",
    "map": "map file",
    "model": "3D model",
    "sound": "sound",
    "texture": "texture",
    "translation": "translation file",
    "other": "file",
}


@dataclass
class AnalysisContext:
    mods: list[Mod]
    order: LoadOrder | None = None
    by_key: dict[str, Mod] = field(default_factory=dict)
    unknown_in_order: list[str] = field(default_factory=list)
    # None means the Steam client was never asked, which is not the same as an
    # empty list. Only a real list makes the comparison meaningful.
    subscribed_ids: list[str] | None = None

    def __post_init__(self) -> None:
        if not self.by_key:
            for mod in self.mods:
                self.by_key.setdefault(mod.key, mod)

    @property
    def has_order(self) -> bool:
        return bool(self.order and self.order.mod_ids)

    def winner_of(self, mod_ids: list[str]) -> str | None:
        """The mod loaded last wins the collision."""
        if not self.has_order:
            return None
        ranked = []
        for mid in mod_ids:
            mod = self.by_key.get(mid.strip().lower())
            if mod and mod.order_index is not None:
                ranked.append((mod.order_index, mid))
        if len(ranked) < 2:
            return None
        return max(ranked)[1]


# --------------------------------------------------------------------------- #
# Metadata rules
# --------------------------------------------------------------------------- #


def rule_duplicate_ids(ctx: AnalysisContext) -> list[Finding]:
    groups: dict[str, list[Mod]] = defaultdict(list)
    for mod in ctx.mods:
        groups[mod.key].append(mod)
    findings = []
    for _key, group in groups.items():
        if len(group) < 2:
            continue
        findings.append(
            Finding(
                rule="duplicate_id",
                severity=Severity.CRITICAL,
                title=f"Duplicate mod id: {group[0].mod_id}",
                detail=(
                    f"{len(group)} folders declare the same id= in their mod.info. "
                    "The engine will load only one of them, and not predictably, so "
                    "any dependency pointing at this id becomes ambiguous."
                ),
                mods=[group[0].mod_id],
                evidence=[str(m.root) for m in group],
                advice="Unsubscribe from the extra copy, or delete the redundant folder.",
            )
        )
    return findings


def rule_missing_dependencies(ctx: AnalysisContext) -> list[Finding]:
    findings = []
    for mod in ctx.mods:
        missing = [r for r in mod.requires if r.strip().lower() not in ctx.by_key]
        if not missing:
            continue
        typos = {r: probable_typo(r, ctx.by_key) for r in missing}
        matched = {r: name for r, name in typos.items() if name}
        # A require= line that only needs punctuation stripped to match an
        # installed mod is a typo in the mod, not a mod you are missing.
        if matched and len(matched) == len(missing):
            pairs = ", ".join(f"{r} -> {name}" for r, name in matched.items())
            findings.append(
                Finding(
                    rule="dependency_typo",
                    severity=Severity.LOW,
                    title=f"{mod.name}: a typo in its own require= line",
                    detail=(
                        f"The mod declares require={', '.join(missing)}, which matches "
                        f"nothing installed. Stripping stray punctuation gives {pairs}, "
                        "and that is installed. The mod author typed this, so the game "
                        "sees the same broken id and will report the dependency as "
                        "missing at load time even though the mod is there."
                    ),
                    mods=[mod.mod_id],
                    evidence=missing,
                    advice=(
                        "Nothing to install. Report it to the mod author, or fix the "
                        "require= line in its mod.info yourself if it bothers the game."
                    ),
                )
            )
            continue
        detail = (
            f"The mod declares require={', '.join(missing)} but no installed mod "
            "carries that id. Depending on the mod this ranges from a harmless "
            "warning at load time to a blocking Lua error."
        )
        if matched:
            pairs = ", ".join(f"{r} looks like {name}" for r, name in matched.items())
            detail += f" Some of these look like typos in the mod's own file: {pairs}."
        findings.append(
            Finding(
                rule="missing_dependency",
                severity=Severity.CRITICAL,
                title=f"{mod.name}: dependency not found",
                detail=detail,
                mods=[mod.mod_id],
                evidence=missing,
                advice="Subscribe to the missing mod, or check it has not been pulled from the Workshop.",
            )
        )
    return findings


def rule_declared_incompatibility(ctx: AnalysisContext) -> list[Finding]:
    findings = []
    for mod in ctx.mods:
        # resolve_requirement, not a raw compare: mod.info is typed by hand and
        # "incompatible=\\TombBodyTex" names a mod that is very much installed.
        clashing = []
        for entry in mod.incompatible:
            found = resolve_requirement(entry, ctx.by_key)
            if found is None:
                continue
            # The real mod id, not the lowercased lookup key. The finding is read
            # by a person and shown next to the other mod names.
            named = getattr(ctx.by_key[found], "mod_id", found)
            if named not in clashing:
                clashing.append(named)
        if not clashing:
            continue
        findings.append(
            Finding(
                rule="declared_incompatibility",
                severity=Severity.CRITICAL,
                title=f"{mod.name} declares itself incompatible with an installed mod",
                detail=(
                    "The author filled in the incompatible= field of their mod.info. This "
                    "is the only case where the incompatibility is asserted by the mod itself."
                ),
                mods=[mod.mod_id] + clashing,
                evidence=clashing,
                advice="Drop one of the two mods from your list.",
            )
        )
    return findings


def rule_dependency_order(ctx: AnalysisContext) -> list[Finding]:
    """A dependency must be loaded before the mod that requires it."""
    if not ctx.has_order:
        return []
    findings = []
    for mod in ctx.mods:
        if mod.order_index is None:
            continue
        for req in mod.requires:
            dep = ctx.by_key.get(req.strip().lower())
            if dep is None:
                continue  # already reported by rule_missing_dependencies
            if dep.order_index is None:
                findings.append(
                    Finding(
                        rule="dependency_disabled",
                        severity=Severity.HIGH,
                        title=f"{mod.name} requires {dep.name}, which is not enabled",
                        detail=(
                            "The required mod is installed on disk but absent from the "
                            "load order being analysed."
                        ),
                        mods=[mod.mod_id, dep.mod_id],
                        evidence=[dep.mod_id],
                        advice=f"Enable {dep.mod_id} and place it before {mod.mod_id}.",
                    )
                )
            elif dep.order_index > mod.order_index:
                findings.append(
                    Finding(
                        rule="dependency_loaded_late",
                        severity=Severity.HIGH,
                        title=f"{dep.name} loads after {mod.name}, which depends on it",
                        detail=(
                            f"Position {dep.order_index + 1} for the dependency against "
                            f"{mod.order_index + 1} for the mod using it. The engine reads mods "
                            "in order, so when the mod initialises its dependency does not "
                            "exist yet."
                        ),
                        mods=[mod.mod_id, dep.mod_id],
                        evidence=[
                            f"{dep.mod_id} at position {dep.order_index + 1}",
                            f"{mod.mod_id} at position {mod.order_index + 1}",
                        ],
                        advice=f"Move {dep.mod_id} above {mod.mod_id} in the list.",
                    )
                )
    return findings


def rule_order_unknown_mods(ctx: AnalysisContext) -> list[Finding]:
    if not ctx.has_order or not ctx.unknown_in_order:
        return []
    return [
        Finding(
            rule="mod_not_installed",
            severity=Severity.HIGH,
            title=f"{len(ctx.unknown_in_order)} mod(s) in the list are not installed",
            detail=(
                "These ids appear in the load order but no matching folder was found. "
                "The game will silently ignore them, or refuse to start the save, "
                "depending on the case."
            ),
            mods=[],
            evidence=ctx.unknown_in_order,
            advice="Check your Steam subscriptions, or clean up the list.",
        )
    ]


# --------------------------------------------------------------------------- #
# File rules
# --------------------------------------------------------------------------- #


def rule_file_collisions(ctx: AnalysisContext) -> list[Finding]:
    """The heart of the tool: two mods ship the same relative path."""
    owners: dict[str, list[Mod]] = defaultdict(list)
    for mod in ctx.mods:
        for rel in mod.assets:
            owners[rel].append(mod)

    # Group by set of mods: forty files shared by the same two mods make one
    # finding, not forty.
    grouped: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
    for rel, mods in owners.items():
        if len(mods) < 2:
            continue
        distinct = sorted({m.mod_id for m in mods})
        # Two folders carrying the same id= necessarily ship the same files:
        # that case belongs to the duplicate-id finding, not this one.
        if len(distinct) < 2:
            continue
        grouped[(classify(rel), tuple(distinct))].append(rel)

    findings = []
    for (category, mod_ids), files in grouped.items():
        severity = COLLISION_SEVERITY.get(category, Severity.LOW)
        # A massive texture collision stays harmless, but a single client Lua
        # collision is enough to break an interface.
        if category in {"texture", "sound", "model"} and len(files) > 50:
            severity = Severity.MEDIUM
        label = CATEGORY_LABEL.get(category, "file")
        winner = ctx.winner_of(list(mod_ids))
        count = len(files)
        plural = "s" if count > 1 else ""
        detail = (
            f"{count} identical {label}{plural} shipped by these mods. The engine stacks "
            "the media folders: only one of these files is actually loaded, the one from "
            "the mod that comes last in the list."
        )
        if category.startswith("lua"):
            advice = (
                "The classic case of two mods replacing the same base-game file instead "
                "of extending it. The losing mod's features disappear with no error "
                "message. Look for a compatibility patch between these two mods, "
                "otherwise you have to pick one."
            )
        elif category == "map":
            advice = "Two maps overlapping: check the cell coordinates."
        else:
            advice = "Mostly cosmetic: the losing mod's visual or sound is ignored."
        if winner:
            detail += f" Here, {winner} wins."

        findings.append(
            Finding(
                rule=f"collision_{category}",
                severity=severity,
                title=f"{label.capitalize()} collision: {' + '.join(mod_ids)}",
                detail=detail,
                mods=list(mod_ids),
                evidence=sorted(files)[:40],
                winner=winner,
                advice=advice,
            )
        )
    return findings


def rule_script_object_collisions(ctx: AnalysisContext) -> list[Finding]:
    """Two mods declare the same script object (item, vehicle, recipe...)."""
    owners: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for mod in ctx.mods:
        for obj in mod.script_objects:
            owners[(obj.kind, obj.fqn)][mod.mod_id].append(obj.source_file)

    grouped: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
    for (kind, fqn), per_mod in owners.items():
        if len(per_mod) < 2:
            continue
        grouped[(kind, tuple(sorted(per_mod)))].append(fqn)

    findings = []
    for (kind, mod_ids), objects in grouped.items():
        significant = kind in SIGNIFICANT_KINDS
        severity = Severity.MEDIUM if significant else Severity.LOW
        if significant and len(objects) > 20:
            severity = Severity.HIGH
        winner = ctx.winner_of(list(mod_ids))
        count = len(objects)
        plural = "s" if count > 1 else ""
        detail = (
            f"{count} object{plural} of kind \"{kind}\" carry the same fully qualified "
            "name in several mods. The engine keeps the last definition it reads, so the "
            "other mod's stats and recipes are overwritten."
        )
        if winner:
            detail += f" Definition kept: the one from {winner}."
        findings.append(
            Finding(
                rule="script_object_collision",
                severity=severity,
                title=f'"{kind}" objects redefined: {" + ".join(mod_ids)}',
                detail=detail,
                mods=list(mod_ids),
                evidence=sorted(objects)[:40],
                winner=winner,
                advice=(
                    "Often deliberate when one of the mods is a rebalance patch. Worth "
                    "checking if the two mods are supposed to be independent."
                ),
            )
        )
    return findings


def rule_workshop_removed(ctx: AnalysisContext) -> list[Finding]:
    """The Workshop item behind an installed mod no longer exists."""
    gone = [m for m in ctx.mods if m.workshop_missing]
    if not gone:
        return []
    return [
        Finding(
            rule="workshop_item_removed",
            severity=Severity.HIGH,
            title=f"{len(gone)} mod(s) are no longer on the Workshop",
            detail=(
                "Steam reports these items as unavailable: removed by the author, "
                "made private, or hidden. Your local copy still works, but nobody "
                "can install them any more, so a server using them will reject "
                "players who do not already have the files."
            ),
            mods=[m.mod_id for m in gone],
            evidence=[f"{m.name} (Workshop {m.workshop_id})" for m in gone],
            advice="Find a maintained replacement before anyone else needs to install it.",
        )
    ]


def rule_workshop_stated_incompatibility(ctx: AnalysisContext) -> list[Finding]:
    """Authors state incompatibilities in prose because no field exists for it."""
    titles: dict[str, str] = {}
    for mod in ctx.mods:
        for name in (mod.workshop_title, mod.name):
            if name and len(name) >= 5:
                titles.setdefault(name.strip().lower(), mod.mod_id)

    findings = []
    for mod in ctx.mods:
        if not mod.workshop_description:
            continue
        hits = [
            other
            for other in stated_incompatibilities(mod.workshop_description, titles)
            if other != mod.mod_id
        ]
        if not hits:
            continue
        findings.append(
            Finding(
                rule="workshop_stated_incompatibility",
                severity=Severity.HIGH,
                title=f"{mod.name} names an installed mod as incompatible",
                detail=(
                    "The Workshop description contains an incompatibility phrase "
                    "followed by the name of a mod you have installed. This is read "
                    "from prose, not from a structured field, so confirm it on the "
                    "Workshop page before acting on it."
                ),
                mods=[mod.mod_id] + hits,
                evidence=hits,
                advice=f"Open the Workshop page for {mod.name} and read the note in context.",
            )
        )
    return findings


def rule_build_branch(ctx: AnalysisContext) -> list[Finding]:
    """A mod that ships no branch for the build being analysed."""
    findings = []
    for mod in ctx.mods:
        if not mod.branch_warning:
            continue
        findings.append(
            Finding(
                rule="no_branch_for_build",
                severity=Severity.MEDIUM,
                title=f"{mod.name} has no folder for this build",
                detail=(
                    f"{mod.branch_warning}. A Build 42 mod ships one folder per game "
                    "version; the game loads the one matching its own version. When "
                    "none matches, the mod may load with the wrong files or not at all."
                ),
                mods=[mod.mod_id],
                evidence=[f"branches present: {', '.join(mod.available_branches) or 'none'}"],
                advice=(
                    "Check whether the author has published an update, or run the tool "
                    "with --build set to the version you actually play."
                ),
            )
        )
    return findings


def rule_unsubscribed_but_installed(ctx: AnalysisContext) -> list[Finding]:
    """Files still on disk for a mod this account no longer subscribes to.

    Steam only deletes them when it next shuts down, and until then the game
    loads them exactly as before. That is why unsubscribing appears to do
    nothing, and why a server can keep running a mod nobody can install.
    """
    if ctx.subscribed_ids is None:
        return []
    subscribed = set(ctx.subscribed_ids)
    orphans = [
        mod
        for mod in ctx.mods
        if mod.workshop_id and mod.workshop_id not in subscribed
    ]
    if not orphans:
        return []
    return [
        Finding(
            rule="installed_but_not_subscribed",
            severity=Severity.HIGH,
            title=f"{len(orphans)} mod(s) are installed but no longer subscribed",
            detail=(
                "Steam does not list these items for this account, yet their files "
                "are still on disk. The game keeps loading them until Steam shuts "
                "down and clears them out, so a mod you unsubscribed from is still "
                "active right now, and nobody else can install it."
            ),
            mods=[m.mod_id for m in orphans],
            evidence=[f"{m.name} (Workshop {m.workshop_id}) at {m.root}" for m in orphans],
            advice=(
                "Close Steam completely and reopen it to let the cleanup run, then "
                "scan again. If the files survive that, delete the folders by hand."
            ),
        )
    ]


def rule_subscribed_but_missing(ctx: AnalysisContext) -> list[Finding]:
    """Subscribed items whose files never arrived."""
    if ctx.subscribed_ids is None:
        return []
    installed = {m.workshop_id for m in ctx.mods if m.workshop_id}
    missing = [wid for wid in ctx.subscribed_ids if wid not in installed]
    if not missing:
        return []
    return [
        Finding(
            rule="subscribed_but_not_installed",
            severity=Severity.MEDIUM,
            title=f"{len(missing)} subscription(s) have no files on disk",
            detail=(
                "Steam lists these items for this account but nothing was found in "
                "the Workshop folder. Usually the download has not run yet, or it "
                "failed. A server listing them in WorkshopItems will stall."
            ),
            mods=[],
            evidence=[f"Workshop {wid}" for wid in missing],
            advice="Let Steam finish downloading, or verify the game files.",
        )
    ]


def rule_parse_errors(ctx: AnalysisContext) -> list[Finding]:
    findings = []
    for mod in ctx.mods:
        if not mod.parse_errors:
            continue
        findings.append(
            Finding(
                rule="partial_read",
                severity=Severity.INFO,
                title=f"{mod.name}: partially read",
                detail="Some files could not be analysed, so findings for this mod are incomplete.",
                mods=[mod.mod_id],
                evidence=mod.parse_errors,
            )
        )
    return findings


ALL_RULES = [
    rule_duplicate_ids,
    rule_missing_dependencies,
    rule_declared_incompatibility,
    rule_dependency_order,
    rule_order_unknown_mods,
    rule_file_collisions,
    rule_script_object_collisions,
    rule_build_branch,
    rule_workshop_removed,
    rule_workshop_stated_incompatibility,
    rule_unsubscribed_but_installed,
    rule_subscribed_but_missing,
    rule_parse_errors,
]


def analyze(
    mods: list[Mod],
    order: LoadOrder | None = None,
    unknown_in_order: list[str] | None = None,
    only_enabled: bool = False,
    subscribed_ids: list[str] | None = None,
    progress=None,
) -> tuple[list[Finding], AnalysisContext]:
    scope = [m for m in mods if m.enabled] if (only_enabled and order) else list(mods)
    ctx = AnalysisContext(
        mods=scope,
        order=order,
        unknown_in_order=unknown_in_order or [],
        subscribed_ids=subscribed_ids,
    )
    findings: list[Finding] = []
    for rule in ALL_RULES:
        produced = rule(ctx)
        log.debug("Rule %s produced %d finding(s)", rule.__name__, len(produced))
        if progress:
            progress(f"Running rule: {rule.__name__.replace('rule_', '')}")
        findings.extend(produced)
    findings.sort(key=lambda f: f.sort_key)
    log.info("Analysis finished: %d finding(s)", len(findings))
    return findings, ctx


def risk_by_mod(findings: list[Finding]) -> dict[str, int]:
    """Cumulative risk score per mod, used to sort the report."""
    scores: dict[str, int] = defaultdict(int)
    for finding in findings:
        for mod_id in dict.fromkeys(finding.mods):
            scores[mod_id] += finding.severity.weight
    return dict(scores)
