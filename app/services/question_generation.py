from typing import Protocol

from app.core.errors import ApiError
from app.schemas.policy import GeneratedQuestions


class QuestionGenerator(Protocol):
    """Provider boundary: return source-grounded questions without saving or approving them."""

    def generate(self, *, policies: list[dict], count: int) -> GeneratedQuestions:
        """Generate a bounded structured batch; adapters must enforce timeout and no secret logging."""
        ...


class UnconfiguredGenerator:
    """Default adapter deliberately performs no network access and produces no fake data."""

    def generate(self, *, policies: list[dict], count: int) -> GeneratedQuestions:
        """Fail explicitly until the team selects and implements a real provider adapter."""
        raise ApiError(503, 'GENERATION_NOT_CONFIGURED', '질문 생성 모델이 아직 연결되지 않았습니다.')


def get_question_generator() -> QuestionGenerator:
    """Inject the provider here once its API, credentials and model have been decided."""
    return UnconfiguredGenerator()
