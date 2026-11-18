"""Apostrophes in argument-bearing Java properties must be literal pairs."""
import pytest

from localize.formats import validation


@pytest.mark.parametrize("source,target,valid", [
    ("Name {0}", "L'adresse {0}", False),
    ("Name {0}", "L'''adresse {0}", False),
    ("Name {0}", "L''adresse {0}", True),
    ("Name {0}", "L''''adresse {0}", True),
    ("Name {0}", "{0} d'abord", False),
    ("Name {0}", "L’adresse {0}", True),
    ("Plain text", "L'adresse", True),
    ("Name {name}", "L'adresse {name}", True),
    ("Count {0,number}", "L'adresse {0,number}", False),
    (r"Name \u007b0\u007d", r"L\u0027adresse {0}", False),
    ("Name {0}", r"L\'adresse {0}", False),
    ("Name {0}", r"L\u0027\u0027adresse {0}", True),
    ("Name {0}", r"L\\u0027adresse {0}", True),
    ("Name {0}", r"L\\\u0027adresse {0}", False),
])
def test_messageformat_apostrophe_check(source, target, valid):
    assert validation.check_messageformat_apostrophes(source, target) is valid
