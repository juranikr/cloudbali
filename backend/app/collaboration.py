from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.config import settings
from app.extended_models import PlaceContributor, UserMessage
from app.models import Favorite, Place, User


def is_admin(user: User) -> bool:
    return user.email.lower() in settings.admin_email_list


def can_edit_place(db: Session, place: Place, user: User) -> bool:
    if is_admin(user) or place.creator_id == user.id:
        return True
    return db.query(PlaceContributor.id).filter(
        PlaceContributor.place_id == place.id,
        PlaceContributor.user_id == user.id,
        PlaceContributor.role == "editor",
    ).first() is not None


def ensure_place_contributor(
    db: Session,
    *,
    place_id: int,
    user_id: int,
    added_by_id: int | None = None,
    role: str = "editor",
) -> PlaceContributor:
    row = db.query(PlaceContributor).filter(
        PlaceContributor.place_id == place_id,
        PlaceContributor.user_id == user_id,
    ).first()
    if row is None:
        row = PlaceContributor(
            place_id=place_id,
            user_id=user_id,
            role=role,
            added_by_id=added_by_id,
        )
        db.add(row)
        db.flush()
    elif row.role != role:
        row.role = role
    return row


def notify_users(
    db: Session,
    user_ids: Iterable[int],
    *,
    kind: str,
    title: str,
    body: str,
    place_id: int | None = None,
    related_event_id: int | None = None,
) -> int:
    unique_ids = {int(user_id) for user_id in user_ids if int(user_id) > 0}
    for user_id in unique_ids:
        db.add(UserMessage(
            user_id=user_id,
            place_id=place_id,
            related_event_id=related_event_id,
            kind=kind[:40],
            title=title[:200],
            body=body,
        ))
    return len(unique_ids)


def place_participant_ids(db: Session, place: Place) -> set[int]:
    ids = {
        row[0]
        for row in db.query(PlaceContributor.user_id).filter(
            PlaceContributor.place_id == place.id
        ).all()
    }
    ids.update(
        row[0]
        for row in db.query(Favorite.user_id).filter(Favorite.place_id == place.id).all()
    )
    if place.creator_id is not None:
        ids.add(place.creator_id)
    return ids
