from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from ..workflow_schemas import Input


class TurnIn(Input):
    operation_id: UUID
    conversation_id: UUID | None = None
    akun_id: str | None = Field(default=None, max_length=64)
    mode: Literal["tanya", "perintah"] = "tanya"
    prompt: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def explicit_command(self):
        self.prompt = self.prompt.strip()
        if not self.prompt:
            raise ValueError("Isi pertanyaan atau perintah")
        if self.mode == "perintah" and not self.akun_id:
            raise ValueError("Pilih satu toko untuk menjalankan perintah")
        return self


class ResolveIn(Input):
    note: str = Field(min_length=5, max_length=500)

    @model_validator(mode="after")
    def meaningful(self):
        self.note = self.note.strip()
        if len(self.note) < 5:
            raise ValueError("Isi hasil pemeriksaan minimal lima karakter")
        return self
