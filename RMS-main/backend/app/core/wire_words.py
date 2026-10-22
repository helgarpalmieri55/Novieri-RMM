"""A wire value (a status, a kind, a source) said inside a sentence.

The English messages print the value as it is on the wire (`closed`, or `'closed'`
when the message used `repr`). The Spanish is the console's own word for it, so a
refusal on a Spanish screen names the state the way the status pill does.

A table maps the wire value to an `N_`-marked catalogue key, which can carry its
own context (`"budget expense status: void"`): the catalogue is flat, so a key of
just `void` would be shared with every other module that says `void`. In English
`say` returns exactly what the message printed before and never touches the
catalogue; in Spanish a value the table does not know is printed as it arrived.

The catalogue holds the running-text word in lowercase ("anulado"). A quoted form
is the console's capitalised word in guillemets, as the console writes it
(`«Anulado»`).
"""

from collections.abc import Iterable, Mapping

from app.core.i18n import locale_var, tr_marked


def say(
    table: Mapping[str, str], value: object, *, quoted: bool = False, marked: bool = False
) -> str:
    """`value` in the current language.

    Bare by default (`void` / `anulado`). `quoted`: English prints it as `repr` did
    (`'void'`) and Spanish as the console's capitalised word in guillemets. `marked`:
    English stays bare and Spanish still uses the guillemets, for a state that reads
    badly after a verb (`está «Borrador»`)."""
    text = str(value)
    if locale_var.get() == "es" and text in table:
        key = table[text]
        word = tr_marked(key)
        if word != key:
            return f"«{word[:1].upper()}{word[1:]}»" if quoted or marked else word
    return repr(value) if quoted else text


def say_list(table: Mapping[str, str], values: Iterable[object]) -> str:
    """A list of wire values as a message printed it: English is the list's own text
    (`['wake']`, byte for byte); Spanish is the console's words, comma-separated."""
    items = list(values)
    if locale_var.get() != "es":
        return str(items)
    return ", ".join(say(table, item) for item in items)
