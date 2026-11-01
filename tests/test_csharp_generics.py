"""Tests for C# generic arguments, field generics, and generic call sites.

Consolidated test suite covering:
- Call-site generic arguments (Invoke<T>(), Register<T, U>(), nested Box<IThing>)
- Field and property generic arguments (List<T>, Dictionary<K, V>, nested generics)
- Unqualified and this-qualified generic callsites resolving to base members (#3406)
- Prevention of fabricated nodes for generic type parameters (T, U, TResult)
"""
from __future__ import annotations

import os
from pathlib import Path
import pytest

from graph_fy.extract import extract


def _refs(tmp_path, files: dict[str, str]) -> set[tuple[str, str]]:
    """Extract, returning {(source_label, target_label)} for  edges."""
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        r = extract([Path(n) for n in files], cache_root=tmp_path / ".cache")
    finally:
        os.chdir(old)
    labels = {n["id"]: n.get("label", "") for n in r["nodes"]}
    return {
        (labels.get(e["source"], ""), labels.get(e["target"], ""))
        for e in r["edges"]
        if e["relation"] == "references"
    }


def _all_refs(tmp_path, files: dict[str, str]) -> list[tuple[str, str, str | None]]:
    """Extract, returning [(source_label, target_label, context)] for every  edge."""
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        r = extract([Path(n) for n in files], cache_root=tmp_path / ".cache")
    finally:
        os.chdir(old)
    labels = {n["id"]: n.get("label", "") for n in r["nodes"]}
    return [
        (labels.get(e["source"], ""), labels.get(e["target"], ""), e.get("context"))
        for e in r["edges"]
        if e["relation"] == "references"
    ]


_TYPES = """\
public interface IThing { }
public interface IService { }
public interface IImpl { }
public class Box<T> { }
public static class StaticHolder
{
    public static void Invoke<T>() { }
    public static void Register<T, U>() { }
}
public class Registry { public void Do<T>() { } }
"""

SETTINGS_CS = """\
namespace Demo
{
    public class Settings
    {
        public T Get<T>(string key) { return default(T); }
        public string GetRaw(string key) { return key; }
    }
}
"""

READER_CS = """\
namespace Demo
{
    public class Reader : Settings
    {
        public int A() { return Get<int>("port"); }
        public int B() { return this.Get<int>("port"); }
        public string C() { return GetRaw("host"); }
        public string D() { return this.GetRaw("host"); }
    }
}
"""

LOCAL_CS = """\
namespace Demo
{
    public class Local
    {
        public T Make<T>() { return default(T); }
        public int E() { return Make<int>(); }
    }
}
"""


def _calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Settings.cs").write_text(SETTINGS_CS, encoding="utf-8")
    (tmp_path / "Reader.cs").write_text(READER_CS, encoding="utf-8")
    (tmp_path / "Local.cs").write_text(LOCAL_CS, encoding="utf-8")
    r = extract(sorted(tmp_path.glob("*.cs")), cache_root=tmp_path)
    return {(e["source"], e["target"]) for e in r["edges"]
            if e["relation"] == "calls"}


def _has(calls, src_frag, tgt_frag):
    return any(src_frag in s and tgt_frag in t for s, t in calls)


# ── Call Site Generic Args ────────────────────────────────────────────────

def test_member_call_with_one_type_argument(tmp_path):
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": "public class Probe { public void A(Registry r) => r.Do<IThing>(); }\n",
    })
    assert (".A()", "IThing") in refs, (
        "member call `recv.Do<T>()` must emit a generic_arg reference to T"
    )

def test_member_call_with_multiple_type_arguments(tmp_path):
    """The Microsoft.Extensions.DependencyInjection shape that the issue calls out."""
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": (
            "public interface IServiceCollection { }\n"
            "public static class Ext\n"
            "{\n"
            "    public static void AddScoped<TService, TImpl>(this IServiceCollection s) { }\n"
            "}\n"
            "public class Probe\n"
            "{\n"
            "    public void A(IServiceCollection s) => s.AddScoped<IService, IImpl>();\n"
            "}\n"
        ),
    })
    assert (".A()", "IService") in refs, (
        "two-arg call must emit a generic_arg reference to the first type argument"
    )
    assert (".A()", "IImpl") in refs, (
        "two-arg call must emit a generic_arg reference to the second type argument"
    )

def test_nested_type_argument_in_call_site(tmp_path):
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": "public class Probe { public void A(Registry r) => r.Do<Box<IThing>>(); }\n",
    })
    assert (".A()", "IThing") in refs, (
        "innermost generic argument in a call site must produce a generic_arg reference"
    )

def test_call_without_type_argument_is_unchanged(tmp_path):
    """A plain call site (no explicit type args) must not regress."""
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": "public class Probe { public void A(Registry r) => r.Do(); }\n",
    })
    # No IThing reference should appear from a type-arg-less call.
    assert not any(src == ".A()" and tgt == "IThing" for src, tgt in refs), (
        "call without type arguments must not invent generic_arg references"
    )

def test_type_parameter_in_call_site_arg_is_not_fabricated(tmp_path):
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": (
            "public class Holder<T>\n"
            "{\n"
            "    public void Use(Registry r) => r.Do<T>();\n"
            "}\n"
        ),
    })
    refs_with_t_target = {(s, t) for s, t in refs if t == "T"}
    assert not refs_with_t_target, (
        "a type parameter (T) used as a call-site type argument must not become a node"
    )

def test_call_site_generic_args_appear_in_issue_repro(tmp_path):
    """End-to-end: the exact two-file repro from #2911 produces all six edges."""
    refs = _refs(tmp_path, {
        "Types.cs": (
            "public interface IAlpha { }\n"
            "public interface IBeta { }\n"
            "public interface IGamma { }\n"
            "public interface IDelta { }\n"
            "public interface IEpsilon { }\n"
            "public interface IZeta { }\n"
            "public class Box<T> { }\n"
            "public class Registry { public void Do<T>() { } }\n"
            "public interface IServiceCollection { }\n"
            "public static class Ext\n"
            "{\n"
            "    public static void AddScoped<TService, TImpl>(this IServiceCollection s) { }\n"
            "}\n"
        ),
        "Probe.cs": (
            "public class Probe\n"
            "{\n"
            "    private Box<IAlpha> _field = null!;\n"
            "    public Box<IBeta> Prop { get; set; } = null!;\n"
            "    public Box<IGamma> Ret() => null!;\n"
            "    public void Param(Box<IDelta> p) { }\n"
            "    public void Call(Registry r) => r.Do<IEpsilon>();\n"
            "    public void Di(IServiceCollection s) => s.AddScoped<IZeta, Box<IZeta>>();\n"
            "}\n"
        ),
    })
    # Call-site positions (5 and 6) -- the field position is covered by the
    # parallel PR #2913, so we focus on what THIS fix is responsible for.
    assert (".Call()", "IEpsilon") in refs, (
        "r.Do<IEpsilon>() must link IEpsilon from the Call method"
    )
    # The DI registration has two type arguments: IZeta and Box<IZeta>.
    # Both the outer IZeta and the inner IZeta (inside Box<...>) must link.
    # Use _all_refs so duplicate (source, target) edges are visible --
    # deduplication is an implementation detail; the bug is a MISSING edge.
    all_di_refs = _all_refs(tmp_path, {
        "Types.cs": (
            "public interface IAlpha { }\n"
            "public interface IBeta { }\n"
            "public interface IGamma { }\n"
            "public interface IDelta { }\n"
            "public interface IEpsilon { }\n"
            "public interface IZeta { }\n"
            "public class Box<T> { }\n"
            "public class Registry { public void Do<T>() { } }\n"
            "public interface IServiceCollection { }\n"
            "public static class Ext\n"
            "{\n"
            "    public static void AddScoped<TService, TImpl>(this IServiceCollection s) { }\n"
            "}\n"
        ),
        "Di.cs": (
            "public class DiHost\n"
            "{\n"
            "    public void Di(IServiceCollection s) => s.AddScoped<IZeta, Box<IZeta>>();\n"
            "}\n"
        ),
    })
    izeta_refs = [tgt for src, tgt, _ctx in all_di_refs if src == ".Di()" and tgt == "IZeta"]
    # The outer IZeta and the inner IZeta (inside Box<...>) are the same
    # reference relationship at the same location, so since #3251 the two
    # walk occurrences collapse into ONE edge at extraction (they were always
    # collapsed by build, and the raw duplicate tripped
    # diagnose_extraction's exact_duplicate_edges warning). The call-site bug
    # this test guards emitted NO edge at all, so existence keeps the teeth.
    assert len(izeta_refs) == 1, (
        "s.AddScoped<IZeta, Box<IZeta>>() must link IZeta exactly once "
        f"(one relationship, no duplicate edge); got {izeta_refs!r}"
    )
    assert (".Di()", "Box") in {(s, t) for s, t, _c in all_di_refs}, (
        "the Box<...> argument itself must still link from the Di method"
    )


# ── Field Generic Args ────────────────────────────────────────────────

def test_field_generic_argument_produces_edge(tmp_path):
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": "public class Probe { private Box<IThing> _f = null!; }\n",
    })
    assert ("Probe", "Box") in refs, "outer field type must still be linked"
    assert ("Probe", "IThing") in refs, "generic argument in field position must be linked"

def test_field_matches_property_behaviour(tmp_path):
    """A field and a property of the same type must produce the same references."""
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": (
            "public class WithField { private Box<IThing> _f = null!; }\n"
            "public class WithProp  { public Box<IThing> P { get; set; } = null!; }\n"
        ),
    })
    field_targets = {t for s, t in refs if s == "WithField"}
    prop_targets = {t for s, t in refs if s == "WithProp"}
    assert field_targets == prop_targets, (
        f"field and property disagree: field={field_targets} property={prop_targets}"
    )

def test_nested_generic_arguments(tmp_path):
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": "public class Probe { private Box<Box<IThing>> _f = null!; }\n",
    })
    assert ("Probe", "IThing") in refs, "innermost generic argument must be linked"

def test_multiple_declarators_share_the_type(tmp_path):
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": "public class Probe { private Box<IThing> _a = null!, _b = null!; }\n",
    })
    assert ("Probe", "IThing") in refs

def test_bare_type_parameter_is_not_fabricated(tmp_path):
    """`T item` must not create a node for the type parameter itself."""
    refs = _refs(tmp_path, {
        "P.cs": "public class Probe<T> { private T _item = default!; }\n",
    })
    assert not any(t == "T" for _, t in refs), "type parameter must not become a node"

def test_type_parameter_as_generic_argument_is_not_fabricated(tmp_path):
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": "public class Probe<T> { private Box<T> _f = null!; }\n",
    })
    assert ("Probe", "Box") in refs, "outer type is still a real reference"
    assert not any(t == "T" for _, t in refs), "type parameter must not become a node"

def test_predefined_types_are_not_fabricated(tmp_path):
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": "public class Probe { private Box<int> _f = null!; private string _s = \"\"; }\n",
    })
    assert not any(t in {"int", "string"} for _, t in refs), (
        "builtin types must not become nodes"
    )

def test_plain_field_still_links(tmp_path):
    """The non-generic path must be unchanged."""
    refs = _refs(tmp_path, {
        "T.cs": _TYPES,
        "P.cs": "public class Probe { private IThing _f = null!; }\n",
    })
    assert ("Probe", "IThing") in refs


# ── Generic Callsites ────────────────────────────────────────────────

def test_unqualified_generic_call_resolves_to_base_member(tmp_path, monkeypatch):
    """The issue's case 1: `Get<int>("port")` with the target on the base class."""
    calls = _calls(tmp_path, monkeypatch)
    assert _has(calls, "reader_a", "settings_get"), sorted(calls)

def test_this_qualified_generic_call_resolves(tmp_path, monkeypatch):
    """Case 2: `this.Get<int>("port")` — the member arm's generic_name name."""
    calls = _calls(tmp_path, monkeypatch)
    assert _has(calls, "reader_b", "settings_get"), sorted(calls)

def test_same_class_generic_call_resolves(tmp_path, monkeypatch):
    """Case 5: `Make<int>()` with the target on the same class."""
    calls = _calls(tmp_path, monkeypatch)
    assert _has(calls, "local_e", "local_make"), sorted(calls)

def test_generic_call_never_targets_the_bracketed_text(tmp_path, monkeypatch):
    """The old failure shape: no target derived from `Get<int>` raw text."""
    calls = _calls(tmp_path, monkeypatch)
    assert not any("<" in t for _, t in calls), sorted(calls)

def test_non_generic_controls_unchanged(tmp_path, monkeypatch):
    """Cases 3 and 4 resolved before the fix and must keep resolving."""
    calls = _calls(tmp_path, monkeypatch)
    assert _has(calls, "reader_c", "settings_getraw"), sorted(calls)
    assert _has(calls, "reader_d", "settings_getraw"), sorted(calls)

