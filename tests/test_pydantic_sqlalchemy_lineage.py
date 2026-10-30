from pathlib import Path
import tempfile
import pytest

from graph_fy.extractors.python import extract_python
from graph_fy.extractors.frameworks import _extract_python_frameworks_regex


def test_pydantic_field_validator_lineage(tmp_path: Path) -> None:
    code = """
from pydantic import BaseModel, field_validator, validator

class UserModel(BaseModel):
    username: str
    email: str

    @field_validator("username")
    def validate_username(cls, v: str) -> str:
        return v.strip().lower()

    @validator("email")
    def validate_email(cls, v: str) -> str:
        return v.strip()
"""
    f = tmp_path / "models.py"
    f.write_text(code, encoding="utf-8")
    result = extract_python(f)
    edges = result.get("edges", [])

    validator_edges = [e for e in edges if e.get("relation") == "validates_field"]
    assert len(validator_edges) >= 2
    assert any("username" in e["target"] for e in validator_edges)
    assert any("email" in e["target"] for e in validator_edges)


def test_sqlalchemy_relationship_bidirectional(tmp_path: Path) -> None:
    code = """
from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    items = relationship("Item", back_populates="owner")

class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"))
    owner = relationship("User", back_populates="items")
"""
    f = tmp_path / "orm.py"
    f.write_text(code, encoding="utf-8")
    result = extract_python(f)
    edges = result.get("edges", [])

    rel_edges = [e for e in edges if e.get("relation") == "relates_to_model"]
    assert len(rel_edges) >= 4  # Bidirectional User <-> Item

    fk_edges = [e for e in edges if e.get("relation") == "foreign_key_to"]
    assert len(fk_edges) >= 1
    assert any("users" in e["target"] for e in fk_edges)


def test_regex_fallback_pydantic_sqlalchemy(tmp_path: Path) -> None:
    code = """
class Account:
    id: int
    name: str

    @field_validator("name")
    def check_name(cls, v):
        return v

    orders = relationship("Order", back_populates="account")
    fk = ForeignKey("accounts.id")
"""
    f = tmp_path / "fallback.py"
    res: dict = {"nodes": [], "edges": []}
    _extract_python_frameworks_regex(f, code, res)
    edges = res["edges"]

    assert any(e["relation"] == "validates_field" and "name" in e["target"] for e in edges)
    assert any(e["relation"] == "relates_to_model" for e in edges)
    assert any(e["relation"] == "foreign_key_to" and "accounts" in e["target"] for e in edges)
