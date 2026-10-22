/**
 * A PERSON'S INITIALS AND A PERSON'S COLOUR, IN ONE PLACE.
 *
 * These two functions existed FOUR TIMES -- `modules/service-desk/people.ts`,
 * `modules/subscriptions/pickerOptions.ts`, `modules/deploy/pickerOptions.ts`
 * and `modules/settings/UsersTab.tsx` -- and the four copies did not agree.
 * That is not a tidiness complaint: the tone is a hash of the id, and it
 * exists so that the same person is the same colour wherever they appear.
 *
 *   - `avatarIndexOf` in subscriptions took the modulus INSIDE the loop
 *     (`hash = (hash * 31 + code) % 6`), which is a different number from the
 *     same hash reduced at the end. So a requester on /deploy and the same
 *     owner on /subscriptions genuinely drew in different colours, which is
 *     the one thing the hash was there to prevent.
 *   - `initialsOf` split on `[\s._@-]+` in three copies and on `\s+` in the
 *     fourth, so `ada.lovelace@toeshee.example` was "AL" on three pages and
 *     "AD" on a fourth.
 *   - `UsersTab`'s took the first TWO words rather than the first and last,
 *     so "Ada King Lovelace" was "AK" there and "AL" everywhere else.
 *
 * The behaviour kept is the majority one, which is also the one with the
 * better answer: an email address is a name with separators in it, and a
 * person's last name is more identifying than their middle one.
 */

/** "Ada Lovelace" -> "AL"; "ada.lovelace@toeshee.example" -> "AL"; "" -> "?". */
export function initialsOf(name: string): string {
  // THE DOMAIN IS NOT PART OF ANYBODY'S NAME. All four copies split on `@`
  // like any other separator, so `ada.lovelace@toeshee.example` -- which is
  // what these pages show when a person has no display name -- came out "AE",
  // the A of "ada" and the E of "example". Every operator at one company
  // therefore shared a last initial. The local part is the name.
  const source = name.trim().split("@")[0] ?? "";
  const words = source
    .split(/[\s._-]+/)
    .filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return (words[0] ?? "").slice(0, 2).toUpperCase();
  return `${words[0]?.[0] ?? ""}${words[words.length - 1]?.[0] ?? ""}`.toUpperCase();
}

/**
 * A stable small integer for an id, so `Avatar`'s tone follows the PERSON and
 * not the row they happen to be on.
 *
 * `Avatar` takes the modulus itself (and handles a negative), so this returns
 * the whole hash rather than a tone number: reducing here and again there
 * would be the same "modulus in two places" defect the copies had.
 */
export function personIndex(id: string): number {
  let hash = 0;
  for (let at = 0; at < id.length; at += 1) hash = (hash * 31 + id.charCodeAt(at)) | 0;
  return Math.abs(hash);
}
