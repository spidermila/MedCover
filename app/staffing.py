"""Read-only condition plan validation and deterministic qualification coverage."""

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from flask import g, has_app_context
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session
from werkzeug.datastructures import MultiDict

from app.extensions import db

if TYPE_CHECKING:
    from app.models.event import Event

from app.models.qualification import Qualification, qualification_parents
from app.models.user import UserAccount


@dataclass
class QualificationGraph:
    qualifications: dict[int, Qualification]
    parents: dict[int, set[int]]
    components: dict[int, int]

    def fillers(self, qualification_id: int) -> set[int]:
        found: set[int] = set()
        pending = [qualification_id]
        while pending:
            node = pending.pop()
            if node not in found:
                found.add(node)
                pending.extend(self.parents.get(node, ()))
        return found


def validate_qualification_graph(parents: dict[int, set[int]]) -> dict[int, int]:
    """Reject directed cycles and return weakly connected hierarchy IDs."""
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(node: int) -> None:
        if node in visiting:
            raise ValueError("Kvalifikační hierarchie nesmí obsahovat cyklus.")
        if node in visited:
            return
        visiting.add(node)
        for parent in sorted(parents.get(node, ())):
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(parents):
        visit(node)
    neighbors = {node: set(edges) for node, edges in parents.items()}
    for node, edges in parents.items():
        for parent in edges:
            neighbors.setdefault(parent, set()).add(node)
    components: dict[int, int] = {}
    for node in sorted(neighbors):
        pending = [node]
        while pending:
            current = pending.pop()
            if current not in components:
                components[current] = node
                pending.extend(neighbors[current])
    return components


@sa_event.listens_for(Session, "after_soft_rollback")
def _invalidate_graph(_session: Session, _context: object) -> None:
    if has_app_context():
        g.pop("staffing_graph", None)


@sa_event.listens_for(Session, "after_flush")
def _invalidate_changed_qualifications(session: Session, context: object) -> None:
    if any(isinstance(obj, Qualification) for obj in session.new | session.dirty | session.deleted):
        _invalidate_graph(session, context)


def qualification_graph() -> QualificationGraph:
    """One graph per request or background app context, invalidated after writes."""
    if has_app_context() and "staffing_graph" in g:
        return g.staffing_graph
    qualifications = {q.id: q for q in db.session.scalars(db.select(Qualification)).all()}
    parents: dict[int, set[int]] = {qid: set() for qid, q in qualifications.items() if not q.is_deleted}
    for child, parent in db.session.execute(db.select(qualification_parents)).all():
        if child in parents and parent in parents:
            parents[child].add(parent)
    graph = QualificationGraph(qualifications, parents, validate_qualification_graph(parents))
    if has_app_context():
        g.staffing_graph = graph
    return graph


def validate_condition_plan(
    minimum: int, maximum: int, requirements: list[tuple[int, int]], *, participant_count: int = 0
) -> None:
    if minimum < 1 or maximum < minimum:
        raise ValueError("Minimum musí být alespoň 1 a maximum nejméně rovné minimu.")
    if maximum < participant_count:
        raise ValueError("Maximum nelze snížit pod současný počet účastníků.")
    graph = qualification_graph()
    seen: set[int] = set()
    totals: dict[int, int] = defaultdict(int)
    has_rp = False
    for qualification_id, count in requirements:
        qualification = graph.qualifications.get(qualification_id)
        if qualification is None or qualification.is_deleted:
            raise ValueError("Podmínka musí používat aktivní kvalifikaci.")
        if qualification_id in seen:
            raise ValueError("Kvalifikace smí být v plánu pouze jednou.")
        if count < 1 or count > maximum:
            raise ValueError("Kvalifikační minimum musí být kladné a nejvýše rovné kapacitě.")
        seen.add(qualification_id)
        totals[graph.components[qualification_id]] += count
        has_rp |= qualification.can_be_rp
    if not has_rp:
        raise ValueError("Alespoň jedna podmínka musí vyžadovat kvalifikaci umožňující roli zodpovědné osoby.")
    if minimum < max(totals.values(), default=0):
        raise ValueError("Minimum účastníků musí pokrýt součet minim v každé kvalifikační hierarchii.")


@dataclass
class RequirementCoverage:
    qualification: Qualification
    minimum_count: int
    participants: list[UserAccount]

    @property
    def covered(self) -> int:
        return len(self.participants)

    @property
    def deficit(self) -> int:
        return self.minimum_count - self.covered


@dataclass
class StaffingSummary:
    participant_count: int
    minimum: int
    maximum: int
    requirements: list[RequirementCoverage]
    rp_valid: bool

    @property
    def free_capacity(self) -> int:
        return max(0, self.maximum - self.participant_count)

    @property
    def people_deficit(self) -> int:
        return max(0, self.minimum - self.participant_count)

    @property
    def is_capacity_full(self) -> bool:
        return self.participant_count >= self.maximum

    @property
    def is_staffing_sufficient(self) -> bool:
        return not self.people_deficit and not any(r.deficit for r in self.requirements) and self.rp_valid


def _evaluate(event: Event, participants: list[UserAccount]) -> StaffingSummary:
    graph = qualification_graph()
    participants = sorted(participants, key=lambda u: str(u.id))
    held = [{q.id for q in user.qualifications if not q.is_deleted} for user in participants]
    coverage = [
        RequirementCoverage(r.qualification, r.minimum_count, [])
        for r in sorted(event.qualification_requirements, key=lambda r: r.qualification_id)
    ]
    hierarchies: dict[int, list[int]] = defaultdict(list)
    for index, requirement in enumerate(coverage):
        component = graph.components.get(requirement.qualification.id)
        if component is not None:
            hierarchies[component].append(index)
    for indexes in hierarchies.values():
        slots = [index for index in indexes for _ in range(min(coverage[index].minimum_count, len(participants)))]
        fillers = {
            index: (
                graph.fillers(coverage[index].qualification.id)
                if not coverage[index].qualification.is_deleted
                else set()
            )
            for index in indexes
        }
        owners: dict[int, int] = {}

        def match(user_index: int, visited: set[int]) -> bool:
            for slot, requirement_index in enumerate(slots):
                if slot in visited or not held[user_index] & fillers[requirement_index]:
                    continue
                visited.add(slot)
                if slot not in owners or match(owners[slot], visited):
                    owners[slot] = user_index
                    return True
            return False

        for user_index in range(len(participants)):
            match(user_index, set())
        for slot, user_index in sorted(owners.items()):
            coverage[slots[slot]].participants.append(participants[user_index])
    rp_valid = any(u.id == event.responsible_person_id and u.is_rp_eligible() for u in participants)
    return StaffingSummary(
        len(participants), event.minimum_participants, event.maximum_participants, coverage, rp_valid
    )


def evaluate_staffing(event: Event) -> StaffingSummary:
    return _evaluate(event, [a.user for a in event.assignments])


def user_helps_staffing(event: Event, user: UserAccount) -> bool:
    participants = [a.user for a in event.assignments]
    if any(u.id == user.id for u in participants):
        return False
    before = _evaluate(event, participants)
    if before.is_capacity_full:
        return False
    after = _evaluate(event, [*participants, user])
    return (
        bool(before.people_deficit)
        or sum(r.covered for r in after.requirements) > sum(r.covered for r in before.requirements)
        or (not before.rp_valid and user.is_rp_eligible())
    )


def condition_plan_from_form(
    form: MultiDict[str, str], *, participant_count: int = 0
) -> tuple[int, int, list[tuple[int, int]]]:
    try:
        minimum = int(form.get("minimum_participants", ""))
        maximum = int(form.get("maximum_participants", ""))
        qualification_ids = form.getlist("requirement_qualification")
        counts = form.getlist("requirement_count")
        if len(qualification_ids) != len(counts):
            raise ValueError
        requirements = [(int(qid), int(count)) for qid, count in zip(qualification_ids, counts)]
    except TypeError, ValueError:
        raise ValueError("Zadejte platná celá čísla pro kapacitu a kvalifikační minima.") from None
    validate_condition_plan(minimum, maximum, requirements, participant_count=participant_count)
    return minimum, maximum, requirements


def can_join_event(event: Event, user: UserAccount) -> bool:
    return (
        user.is_active
        and not user.is_archived
        and user.has_permission("event.assign_own")
        and not event.archived
        and event.status.name == "ASSIGNMENTS_OPEN"
        and (not event.is_centrally_coordinated or user.has_permission("event.assign_other"))
        and len(event.assignments) < event.maximum_participants
        and not any(a.user_id == user.id for a in event.assignments)
    )
