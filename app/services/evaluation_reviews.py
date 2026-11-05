from sqlalchemy import func, select

from app.models.evaluation import EvaluationReview
from app.services.policy import iso


def review_data(review):
    return {'id': review.id, 'itemId': review.item_id, 'revision': review.revision,
            'side': review.side, 'conditionId': review.condition_id, 'verdict': review.verdict,
            'reason': review.reason, 'reviewedBy': review.reviewed_by, 'createdAt': iso(review.created_at)}


def latest_reviews(db, item_ids):
    """One bounded map for the selected items; full audit history has a separate paged API."""
    latest = {}
    newest = select(EvaluationReview.item_id, EvaluationReview.side, EvaluationReview.condition_id,
        func.max(EvaluationReview.revision).label('revision')).where(EvaluationReview.item_id.in_(item_ids))\
        .group_by(EvaluationReview.item_id, EvaluationReview.side, EvaluationReview.condition_id).subquery()
    rows = db.scalars(select(EvaluationReview).join(newest,
        (EvaluationReview.item_id == newest.c.item_id) & (EvaluationReview.revision == newest.c.revision))).all()
    for row in rows:
        latest[(row.item_id, row.side, row.condition_id)] = review_data(row)
    return latest


def answer_review_data(review):
    return {'id': review.id, 'itemId': review.item_id, 'revision': review.revision,
            'side': review.side, 'verdict': review.verdict, 'reason': review.reason,
            'reviewedBy': review.reviewed_by, 'createdAt': iso(review.created_at)}


def latest_answer_reviews(db, item_ids):
    from app.models.evaluation import EvaluationAnswerReview
    model = EvaluationAnswerReview
    newest = select(model.item_id, model.side, func.max(model.revision).label('revision'))\
        .where(model.item_id.in_(item_ids)).group_by(model.item_id, model.side).subquery()
    rows = db.scalars(select(model).join(newest,
        (model.item_id == newest.c.item_id) & (model.revision == newest.c.revision))).all()
    return {(row.item_id, row.side): answer_review_data(row) for row in rows}
