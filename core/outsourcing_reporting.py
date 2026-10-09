"""Separate external custody transfers from actual subcontract consumption."""
from sqlalchemy import func

from database import OutsourcingContainer, OutsourcingJob, OutsourcingMovement, OutsourcingShipmentLine


def transfer_transaction_ids(db):
    return db.query(OutsourcingShipmentLine.transaction_id)


def movement_totals(db, start, end=None, domain=None):
    query = (db.query(OutsourcingContainer.item_id, OutsourcingMovement.kind,
                      func.sum(OutsourcingMovement.quantity), func.count(OutsourcingMovement.id))
             .join(OutsourcingMovement, OutsourcingMovement.container_id == OutsourcingContainer.id)
             .join(OutsourcingJob, OutsourcingJob.id == OutsourcingContainer.job_id)
             .filter(OutsourcingMovement.kind.in_(("consumption", "waste")),
                     OutsourcingMovement.created_at >= start))
    if end is not None:
        query = query.filter(OutsourcingMovement.created_at < end)
    if domain is not None:
        query = query.filter(OutsourcingJob.domain == domain)
    totals = {}
    for item_id, kind, quantity, count in query.group_by(OutsourcingContainer.item_id, OutsourcingMovement.kind).all():
        entry = totals.setdefault(item_id, {"consumption": 0.0, "waste": 0.0, "count": 0})
        entry[kind] += float(quantity or 0)
        entry["count"] += count
    return totals
