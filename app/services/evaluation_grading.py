"""Frozen numbering and lightweight tiles, reusing #37's O/X rules and review storage."""
from collections import Counter

from sqlalchemy import select

from app.core.errors import ApiError
from app.models.evaluation import EvaluationItem
from app.services.evaluation_analysis import reviewed_side, SCREEN_COUNTS
from app.services.evaluation_reviews import latest_reviews, latest_answer_reviews
from app.services.evaluations import TERMINAL


def navigation(db, run, question_id):
    by_question = dict(db.execute(select(EvaluationItem.question_id, EvaluationItem.id)
        .where(EvaluationItem.evaluation_id == run.id)).all())
    sequence = [by_question[question['id']] for question in run.inputs['testSet']['questions']
                if question['id'] in by_question]
    item_id = by_question.get(question_id)
    if item_id not in sequence:
        return {'previousItemId': None, 'nextItemId': None}
    index = sequence.index(item_id)
    return {'previousItemId': sequence[index - 1] if index else None,
            'nextItemId': sequence[index + 1] if index + 1 < len(sequence) else None}


def board(db, run):
    if run.inputs.get('mode') != 'SINGLE':
        raise ApiError(409, 'SINGLE_EVALUATION_REQUIRED', '채점 화면은 단일 대상 평가를 선택하세요.')
    # No answer bodies, retrieved documents, or B output are loaded for the grid.
    rows = db.execute(select(EvaluationItem.id, EvaluationItem.question_id, EvaluationItem.input,
        EvaluationItem.review_revision,
        EvaluationItem.a['callStatus'].as_string().label('call_status'),
        EvaluationItem.a['criteria'].label('criteria'),
        EvaluationItem.a['conditionResults'].label('conditions'))
        .where(EvaluationItem.evaluation_id == run.id)).all()
    order = {question['id']: index for index, question in enumerate(run.inputs['testSet']['questions'], 1)}
    if (len(rows) != len(order) or len(rows) != run.total_questions
            or len({row.question_id for row in rows}) != len(rows) or any(row.question_id not in order for row in rows)):
        raise ApiError(409, 'EVALUATION_SNAPSHOT_MISMATCH', '확정 시험지와 평가 문항 구성이 다릅니다.')
    ids = [row.id for row in rows]
    reviews, overall = latest_reviews(db, ids), latest_answer_reviews(db, ids)
    grouped = {}
    for (item_id, side, _), review in reviews.items():
        if side == 'a':
            grouped.setdefault(item_id, []).append(review)
    items = []
    for row in sorted(rows, key=lambda row: order[row.question_id]):
        side = {'callStatus': row.call_status, 'criteria': row.criteria or {},
                'conditionResults': row.conditions or []} if row.call_status else None
        detail = reviewed_side(row.input, side, grouped.get(row.id, []), overall.get((row.id, 'a')),
            expected_criteria=run.inputs['criteria']['definitions'])
        available = row.call_status == 'SUCCEEDED'
        items.append({'id': row.id, 'questionId': row.question_id, 'questionNumber': order[row.question_id],
            'category': row.input.get('category'), 'screenStatus': detail['screenStatus'],
            'derivedScreenStatus': detail['derivedScreenStatus'], 'callStatus': row.call_status or 'NOT_RUN',
            'answerAvailable': available, 'canReview': available and run.status in TERMINAL,
            'needsReview': detail['needsReview'], 'reviewRevision': row.review_revision,
            'answerReview': detail['answerReview'], 'reviewConflict': detail['reviewConflict']})
    counts = Counter(item['screenStatus'] for item in items)
    result = {value: counts[key] for key, value in SCREEN_COUNTS.items()} | {'total': len(items)}
    processed = len(items) - result['remaining'] - result['pending']
    prompt = run.inputs.get('prompts', {}).get('a') or {}
    return {'evaluationId': run.id, 'evaluationStatus': run.status,
        'testSetVersionId': run.inputs['testSet']['testSetVersionId'],
        'promptVersionId': prompt.get('id'), 'promptVersion': prompt.get('version'),
        'useConfiguredPrompt': run.inputs.get('useConfiguredPrompt', False),
        'criteriaVersion': run.inputs['criteria']['version'], 'counts': result,
        'gradedQuestions': result['correct'] + result['incorrect'], 'processedQuestions': processed,
        'notRunQuestions': result['remaining'] if run.status in TERMINAL else 0,
        'canOpenWrongAnswers': run.status in TERMINAL and processed > 0, 'items': items}
