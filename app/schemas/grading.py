"""A lightweight Figma grading grid over the existing #37 result/review contract."""
from pydantic import BaseModel

from app.schemas.condition_evaluation import AnswerReviewOut
from app.schemas.evaluation import ScreenCounts, ScreenStatus


class GradingTile(BaseModel):
    id: str
    questionId: str
    questionNumber: int
    category: str | None
    screenStatus: ScreenStatus
    derivedScreenStatus: ScreenStatus
    callStatus: str
    answerAvailable: bool
    canReview: bool
    needsReview: bool
    reviewRevision: int
    answerReview: AnswerReviewOut | None
    reviewConflict: bool


class GradingBoard(BaseModel):
    evaluationId: str
    evaluationStatus: str
    testSetVersionId: str
    promptVersionId: str | None
    promptVersion: int | None
    useConfiguredPrompt: bool
    criteriaVersion: int
    counts: ScreenCounts
    gradedQuestions: int
    processedQuestions: int
    notRunQuestions: int
    canOpenWrongAnswers: bool
    items: list[GradingTile]
