---
name: attrs-classes
description: This project's convention for writing classes — use the attrs library (modern @define/@frozen API) instead of plain classes or dataclasses. Use whenever defining, reviewing, or refactoring a class that holds data or configuration.
---

# Writing classes with attrs

**Default rule for this project: write classes with `attrs`, not hand-written `__init__`/`__eq__`/`__repr__` boilerplate and not `@dataclasses.dataclass`.** Use the modern ("next-generation") API imported from the `attrs` namespace. Verify the installed version's API with the fact-checker if unsure; the API below reflects attrs 26.x.

## The API to use
- `from attrs import define, frozen, field` (and `evolve`, `validators`, `converters`, `Factory` as needed).
- `@define` — the standard mutable class. Modern defaults: **slots on**, type-annotation aware, correct MRO attribute collection, auto-detect of hand-written methods.
- `@frozen` — immutable class (`@define(frozen=True)`). Prefer this for value objects, configs, and anything used as a dict key / set member.
- `field(...)` — per-attribute configuration (defaults, validators, converters, etc.). Only needed when an attribute needs more than a bare type annotation.

Do **not** use the legacy `@attr.s`/`attr.ib` API in new code (it stays supported, but this project standardizes on `@define`/`@frozen`). Don't mix `@dataclass` and `attrs` for the same role.

## Basic shape
```python
from attrs import define, frozen, field

@define
class Point:
    x: int
    y: int

@frozen
class Config:
    host: str
    port: int = 8080            # default
    retries: int = field(default=3)
```
Bare annotations become fields automatically — reach for `field()` only when configuring the attribute.

## Common patterns
- **Mutable defaults** → use a factory, never a shared mutable literal:
  ```python
  tags: list[str] = field(factory=list)          # NOT default=[]
  ```
- **Validation** at construction (and on assignment for `@define`):
  ```python
  from attrs import define, field, validators
  @define
  class Server:
      port: int = field(validator=validators.and_(validators.instance_of(int), validators.gt(0)))
  ```
- **Coercion** of inputs with a converter:
  ```python
  path: Path = field(converter=Path)
  ```
- **Keyword-only** fields: `field(kw_only=True)` (or `@define(kw_only=True)` for all).
- **Derived field** on a frozen class — set via `object.__setattr__` in `__attrs_post_init__` (you can't assign directly on a frozen instance).
- **Immutable updates:** `attrs.evolve(instance, field=new_value)` returns a changed copy (on 3.13+ `copy.replace()` also works).
- **Exceptions:** `@define` works for custom exception classes too.

## Why this default
Less boilerplate, slotted classes by default (lower memory, attribute typo protection), validators/converters at the boundary, and `evolve` for clean immutable updates. It pairs well with the type checker — annotations on fields are real type information.

## Interaction with the rest of the toolchain
- **Type checking:** attrs ships with type stubs and a Pyright/mypy plugin story, so `@define`/`@frozen` classes type-check correctly (the generated `__init__` signature is understood). Keep field annotations precise.
- **CLI layer:** good fit for parsed-config / parameter objects passed from the thin click layer into core logic.
- **Tests:** the generated `__eq__`/`__repr__` make assertions easy — compare whole instances rather than field-by-field.

## When NOT to force attrs
Plain functions don't need a class. Tiny, short-lived local records can stay as a `NamedTuple` if that reads better. A class that is purely behavior (no data) doesn't need `@define`. Use judgment — the rule targets data/config/value classes, which is most of them.
