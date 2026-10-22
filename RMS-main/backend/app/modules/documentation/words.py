"""The console's words for the wire values the documentation messages say
(`enums.DocLinkTarget` and `enums.DocRecordType` in `frontend/src/i18n/es/enums.ts`).

`say(table, value)` (app/core/wire_words.py) returns the wire value itself in
English, so every English message is what it always was.
"""

from app.core.i18n import N_

# With the article, so "no se encontró el dispositivo X" agrees.
LINK_TARGET_WORDS = {
    "device": N_("documentation link target: device"),
    "device_group": N_("documentation link target: device_group"),
    "credential": N_("documentation link target: credential"),
    "record": N_("documentation link target: record"),
    "ipam_subnet": N_("documentation link target: ipam_subnet"),
    "ipam_address": N_("documentation link target: ipam_address"),
}
RECORD_TYPE_WORDS = {
    "network": N_("documentation record type: network"),
    "service": N_("documentation record type: service"),
    "vendor": N_("documentation record type: vendor"),
    "location": N_("documentation record type: location"),
}
