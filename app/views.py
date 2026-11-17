"""Shared response contracts for Dopes clients and the public OpenAPI document."""
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .catalog import Board


class Person(BaseModel):
    uuid: str
    id: str
    display_name: str
    pfp_url: str | None = None
    fallback_pfp_url: str | None = None
    trust: int = 0
    blocked: bool = False
    local_id: int | None = None
    sent: int = 0
    received: int = 0
    score: int = 0
    last_invited_at: str | None = None


class People(BaseModel):
    items: list[Person]


class Connection(BaseModel):
    connected: bool


class Contacts(People, Connection):
    pass


class InvitationState(BaseModel):
    id: str
    state: Literal["pending", "accepted", "declined", "revoked"]


class Invitation(InvitationState):
    board: str
    board_name: str
    sender: Person
    recipient: Person
    created_at: str


class Invitations(BaseModel):
    items: list[Invitation]


class File(BaseModel):
    id: str
    name: str
    board: str
    board_name: str
    uploader: Person
    size: int
    created_at: str
    download_url: str


class Files(BaseModel):
    items: list[File]
    total: int


class BoardUser(BaseModel):
    id: int
    username: str
    display_name: str
    color: str


class Category(BaseModel):
    id: int
    name: str
    color: str


class Dope(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    title: str
    description: str
    description_html: str
    status: Literal["active", "saved", "review", "completed", "archived"]
    category: Category | None
    time_minutes: int
    assigned_to: BoardUser | None
    completed_by: BoardUser | None
    created_at: str
    completed_at: str | None
    completion_description: str
    comment_count: int
    dependencies: list[dict]
    dependents: list[dict]
    versions: list[dict]
    assignment_history: list[dict]
    review: dict


class Workspace(BaseModel):
    board: Board
    viewer: BoardUser
    categories: list[Category]
    dopes: list[Dope]
    progress: list[dict]
