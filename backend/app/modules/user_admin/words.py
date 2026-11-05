"""The words the user-administration refusals say about what was being done.

`say(table, value)` (app/core/wire_words.py) returns the English phrase itself
in English, so every English message is what it always was. In Spanish the
phrase is an infinitive that fits the sentence it is printed in; the console's
own words are "Deshabilitar", "Eliminar", "Quitar" and "invitación"
(`settings.users` in `frontend/src/i18n/es/settings.ts`).

Two tables, because the two sentences need two grammars:

- `ACTION_WORDS`: after "you cannot ... your own account".
- `OUTCOME_WORDS`: after "cannot be ..." (the English participle; the Spanish
  infinitive of "no se puede ...").

(A third, for "the identity provider ... to ...", went with the identity
provider's calls in P2 phase 1 Task 8.)
"""

from app.core.i18n import N_

# The wire words the refusal helpers are called with. Constants, so a call such as
# `_refuse_self(actor, user, DISABLE)` reads as what it is: a word the tables below
# say, not a sentence handed to a raiser.
DISABLE = "disable"
DELETE = "delete"
RESET_FACTORS = "reset the sign-in methods of"
CANNOT_BE_DISABLED = "disabled"
CANNOT_BE_DELETED = "deleted"
CANNOT_BE_STRIPPED = "stripped of the role"

ACTION_WORDS = {
    DISABLE: N_("user admin, you cannot ... your own account: disable"),
    DELETE: N_("user admin, you cannot ... your own account: delete"),
    RESET_FACTORS: N_("user admin, you cannot ... your own account: reset the sign-in methods of"),
}

OUTCOME_WORDS = {
    CANNOT_BE_DISABLED: N_("user admin, cannot be ...: disabled"),
    CANNOT_BE_DELETED: N_("user admin, cannot be ...: deleted"),
    CANNOT_BE_STRIPPED: N_("user admin, cannot be ...: stripped of the role"),
}
