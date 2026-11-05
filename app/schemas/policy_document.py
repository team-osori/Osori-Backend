"""Reviewable source locations; these are client-returned metadata, not attestations."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocumentChunk(BaseModel):
    model_config = ConfigDict(extra='forbid')
    index: Annotated[int, Field(strict=True, ge=1, le=200)]
    pageNumber: Annotated[int | None, Field(strict=True, ge=1, le=50)] = None
    startOffset: Annotated[int, Field(strict=True, ge=0, le=100000)]
    endOffset: Annotated[int, Field(strict=True, ge=1, le=100000)]
    method: Literal['text', 'ocr']


class DocumentSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    documentId: Annotated[str, Field(pattern=r'^doc_[a-f0-9]{64}$')]
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    contentHash: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    pageCount: Annotated[int | None, Field(strict=True, ge=1, le=50)] = None
    chunkCount: Annotated[int, Field(strict=True, ge=1, le=200)]
    chunks: Annotated[list[DocumentChunk], Field(min_length=1, max_length=200)]
    ocrPages: Annotated[list[int], Field(max_length=50)] = []
    emptyPages: Annotated[list[int], Field(max_length=50)] = []
    imageTextPages: Annotated[list[int], Field(max_length=50)] = Field(default=[],
        description="텍스트를 우선 사용해 이미지 본문이 누락될 수 있는 페이지. 원문 검토 후 필요하면 force OCR로 다시 업로드하세요.")

    @model_validator(mode='after')
    def valid_locations(self):
        if self.chunkCount != len(self.chunks):
            raise ValueError('청크 수가 일치하지 않습니다.')
        for pages in (self.ocrPages, self.emptyPages, self.imageTextPages):
            if pages != sorted(set(pages)) or any(type(p) is not int or not self.pageCount or not 1 <= p <= self.pageCount for p in pages):
                raise ValueError('페이지 번호가 유효하지 않습니다.')
        if set(self.imageTextPages) & (set(self.ocrPages) | set(self.emptyPages)):
            raise ValueError('이미지 검토 페이지 정보가 일치하지 않습니다.')
        previous_end, previous_page = 0, 0
        for index, chunk in enumerate(self.chunks, 1):
            if chunk.index != index or not previous_end <= chunk.startOffset < chunk.endOffset:
                raise ValueError('청크 순서나 범위가 유효하지 않습니다.')
            if self.pageCount is None:
                if chunk.pageNumber is not None or chunk.method != 'text':
                    raise ValueError('페이지 정보는 PDF에만 지정합니다.')
            elif chunk.pageNumber is None or not previous_page <= chunk.pageNumber <= self.pageCount:
                raise ValueError('청크 페이지가 유효하지 않습니다.')
            if chunk.pageNumber in self.emptyPages or (chunk.method == 'ocr') != (chunk.pageNumber in self.ocrPages):
                raise ValueError('추출 방식과 페이지 정보가 일치하지 않습니다.')
            previous_end, previous_page = chunk.endOffset, chunk.pageNumber or 0
        return self
