# Source dossier: replacing Keycloak with first-party authentication

Research date: 2026-10-31 (the "today" of the research session). Web research only; the repository was not touched.

## How to read this dossier

- Every quote was copied from text I fetched myself during this session (curl from the publisher's own host, then text extraction; PDFs via pdftotext). Whitespace is normalised, and a line-break hyphen inside a word is joined (for example "memory- constrained" is written "memory-constrained"). `[...]` marks text I left out. I did not quote anything from memory.
- "Fetch" = the URL answered HTTP 200 and I read the text. Anything I could not fetch or could not find is listed under UNVERIFIED at the end.
- Where two sources disagree, both are recorded and the newer one is marked.
- Section numbers (for example "NIST 3.2.2") are the document's own.
- This dossier does not design anything. Lines starting "Note:" are factual observations about the sources (version gaps, conflicts), not recommendations.
- GitHub's REST API was not reachable from this session ("GitHub access to this repository is not enabled for this session"). I did not attach any repository, because the task is web-only. Maintenance status therefore comes from PyPI/npm release history and raw files on raw.githubusercontent.com, not from repository metadata such as "archived" or open-issue counts.

---

## Topic 1. NIST SP 800-63B (latest revision)

### [S1] NIST SP 800-63B-4, Digital Identity Guidelines: Authentication and Authenticator Management — NIST — https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf (DOI https://doi.org/10.6028/NIST.SP.800-63B-4) — Revision 4, July 2025 (final)

Status of the revision:
- Title page: "NIST Special Publication NIST SP 800-63B-4 Digital Identity Guidelines Authentication and Authenticator Management [...] July 2025". It is the final publication, not a draft (confirmed on the CSRC page [S2]).

Passwords (3.1.1.2):
- Length: "Verifiers and CSPs SHALL require passwords that are used as a single-factor authentication mechanism to be a minimum of 15 characters in length. Verifiers and CSPs MAY allow passwords that are only used as part of multi-factor authentication processes to be shorter but SHALL require them to be a minimum of eight characters in length."
- Maximum: "Verifiers and CSPs SHOULD permit a maximum password length of at least 64 characters."
- Character set: "Verifiers and CSPs SHOULD accept all printing ASCII [RFC20] characters and the space character in passwords." and "Each Unicode code point SHALL be counted as a single character when evaluating password length."
- Composition: "Verifiers and CSPs SHALL NOT impose other composition rules (e.g., requiring mixtures of different character types) for passwords."
- Rotation: "Verifiers and CSPs SHALL NOT require subscribers to change passwords periodically. However, verifiers SHALL force a change if there is evidence that the authenticator has been compromised."
- Hints and KBA: "Verifiers and CSPs SHALL NOT permit the subscriber to store a hint (e.g., a reminder of how the password was created) that is accessible to an unauthenticated claimant." and "Verifiers and CSPs SHALL NOT prompt subscribers to use knowledge-based authentication (KBA) (e.g., "What was the name of your first pet?") or security questions when choosing passwords."
- Blocklist: "When processing a request to establish or change a password, verifiers SHALL compare the prospective secret against a blocklist that contains known commonly used, expected, or compromised passwords. The entire password SHALL be subject to comparison, not substrings or words that might be contained therein. For example, the list may include: • Passwords obtained from previous breach corpuses • Dictionary words • Context-specific words, such as the name of the service, the username, and derivatives thereof"
- Rate limiting is required for passwords: "Verifiers SHALL implement a rate-limiting mechanism that effectively limits the number of failed authentication attempts that can be made on the subscriber account, as described in Sec. 3.2.2."
- Password managers: "Verifiers SHALL allow the use of password managers and autofill functionality. Verifiers SHOULD permit claimants to use the "paste" function when entering a password to facilitate password manager use when password autofill APIs are unavailable."
- Storage: "Verifiers SHALL store passwords in a form that is resistant to offline attacks. Passwords SHALL be salted and hashed using a suitable password hashing scheme." and "The salt SHALL be at least 32 bits in length and chosen to minimize salt value collisions among stored hashes" and "The chosen cost factor SHOULD be as high as practical without negatively impacting verifier performance. It SHOULD be increased over time to account for increases in computing performance." and "In addition, verifiers SHOULD perform an additional iteration of a keyed hashing or encryption operation using a secret key known only to the verifier."

Rate limiting / throttling (3.2.2):
- "the verifier SHALL limit consecutive failed authentication attempts using a specific authenticator on a single subscriber account to no more than 100 by disabling that authenticator."
- "The limit of 100 attempts is an upper bound, and agencies MAY impose lower limits."
- "Authenticators that have been disabled SHALL be required to rebind to the subscriber account, as described in Sec. 4.1, to be usable in the future."
- Lock-out mitigation options: "Additional techniques MAY be used to reduce the likelihood that an attacker will lock the legitimate claimant out due to rate limiting. These include: • Requiring the claimant to complete a bot detection and mitigation challenge before attempting authentication [...] • Requiring the claimant to wait after a failed attempt for a period of time that increases as the subscriber account approaches its maximum allowance for consecutive failed attempts (e.g., 30 seconds up to an hour) • Leveraging other risk-based or adaptive authentication techniques [...]"
- Reset: "When the subscriber successfully authenticates, the verifier SHOULD disregard any previous failed attempts for the authenticators used in the successful authentication."

Out-of-band and email (3.1.3.1):
- "Email SHALL NOT be used for out-of-band authentication because it may be vulnerable to: • Access using only a password • Interception in transit or at intermediate mail servers • Rerouting attacks, such as those caused by Domain Name System (DNS) spoofing"
- Carve-out: "Confirmation codes that are sent to validate email addresses or are issued as recovery codes (see Sec. 4.2.1.2) are not authentication processes and not affected by the above prohibition."
- Out-of-band secrets under 64 bits: "If the authentication secret is less than 64 bits long, the verifier SHALL implement a rate-limiting mechanism that effectively limits the total number of consecutive failed authentication attempts that can be made on the subscriber account as described in Sec. 3.2.2. Generating a new authentication secret SHALL NOT reset the failed authentication count."
- SMS/PSTN is restricted, not banned: "Use of the PSTN for out-of-band verification is restricted as described in this section" (3.1.3.3).

One-time passwords (3.1.4):
- Replay: "Verifiers SHALL accept a given OTP only once while it is valid to provide replay resistance, as described in Sec. 3.2.7."
- TOTP lifetime: "Time-based OTPs [TOTP] SHALL have a defined lifetime that is determined by the expected clock drift in either direction of the authenticator over its lifetime plus an allowance for network delay and claimant entry of the OTP."

AAL2 (2.2):
- "At AAL2, authentication SHALL use either a multi-factor authenticator or a combination of two separate authentication factors."
- Password plus a physical authenticator is allowed: "When a combination of two single-factor authenticators is used, the combination SHALL include one physical authenticator (i.e., "something you have") from the following list in conjunction with either a password (Sec. 3.1.1) or a biometric comparison: • Look-up secret (Sec. 3.1.2) • Out-of-band device (Sec. 3.1.3) • Single-factor OTP (Sec. 3.1.4) • Single-factor cryptographic authentication (Sec. 3.1.6)"
- Phishing resistance must be offered: "Verifiers SHALL offer at least one phishing-resistant authentication option at AAL2, as described in Sec. 3.2.5." and "In all cases, verifiers SHOULD encourage the use of phishing-resistant authentication at AAL2 whenever practical since phishing is a significant threat vector [IC3]."
- Reauthentication at AAL2 (2.2.3): "A definite reauthentication overall timeout SHALL be established, which SHOULD be no more than 24 hours at AAL2. The inactivity timeout SHOULD be no more than 1 hour. When the inactivity timeout has occurred but the overall timeout has not yet occurred, the verifier MAY allow the subscriber to reauthenticate using only a successful password or biometric comparison in conjunction with the session secret, as described in Sec. 5.1."
- For comparison, AAL3 (2.3.3): "At AAL3, the overall timeout for reauthentication SHALL be no more than 12 hours. The inactivity timeout SHOULD be no more than 15 minutes." AAL1 (2): "A definite reauthentication overall timeout SHALL be established, which SHOULD be no more than 30 days at AAL1."
- AAL3 definition: "AAL3 authentication requires a phishing-resistant authenticator (see Sec. 3.2.5) with a non-exportable authentication key (see Sec. 3.2.13)."

Syncable authenticators (Appendix B, which the document labels normative):
- "Appendix B discusses syncable authenticators. This appendix is normative."
- "The ability to "sync" authenticators — specifically to copy (i.e., clone) their authentication secrets to the cloud and thence to additional authenticators — is a relatively new development in authentication."
- "User access to authentication keys in the sync fabric SHALL be protected by AAL2-equivalent MFA to preserve the integrity of the authentication protocols using the synced keys."
- "Since syncable authenticators (described in Appendix B) require the private key to be exportable, syncable authenticators SHALL NOT be used at AAL3."
- AAL2 use is contemplated (B.5): "A common use of syncable authenticators is in an AAL2 authentication transaction. The following items summarize how WebAuthn syncable authenticators satisfy aspects of AAL2 requirements:"
- Note: the extracted text of the whole document contains no occurrence of the word "passkey"; the document says "syncable authenticators".

Sessions and cookies (5.1, 5.1.1, 5.2):
- "Cookies used for session maintenance: 1. SHALL be tagged to be accessible only on secure (i.e., HTTPS) sessions. 2. SHALL be accessible to the minimum practical hostnames and paths. 3. SHOULD be tagged as inaccessible via JavaScript (i.e., HttpOnly). 4. SHOULD be tagged to expire at or soon after the session's validity period. [...] 5. SHOULD have the "__Host-" prefix and set "Path=/". 6. SHOULD set "SameSite=Lax" or "SameSite=Strict". 7. SHOULD contain only an opaque string (e.g., a session identifier) and SHALL NOT contain cleartext personal information."
- "Cookies and similar "remember my browser" features SHALL NOT be used instead of authentication except as provided for reauthentication at AAL2 in Sec. 2.2.3 when the inactivity limit has been exceeded but the time limit has not."
- Access tokens vs presence (5.1.2): "The RP SHALL NOT interpret the presence of an access token as an indicator of the subscriber's presence in the absence of other signals."

Recovery (4.2.3): "In all cases, account recovery SHALL cause a notification to be sent to the subscriber or their designee, as described in Sec. 4.6."

### [S2] NIST CSRC publication page for SP 800-63B-4 — NIST Computer Security Resource Center — https://csrc.nist.gov/pubs/sp/800/63/b/4/final — read 2026-10-31
- "Date Published: July 2025"; "Supersedes: SP 800-63B (03/02/2020)".
- "Document History: 12/16/22: SP 800-63B-4 (Draft) 08/21/24: SP 800-63B-4 (Draft) 07/31/25: SP 800-63B-4 (Final)".
- "Planning Note ( 10/06/2026 ): The Implementation Resource Hub for SP 800-63-4 is now available!"
- Conclusion (verified): Revision 4 is final, dated July 2025 (final version 07/31/25).

### [S3] NIST CSRC page for the withdrawn SP 800-63B (Rev. 3 with updates) — NIST CSRC — https://csrc.nist.gov/pubs/sp/800/63/b/upd2/final — read 2026-10-31
- "NIST SP 800-63B Withdrawn on August 01, 2025. Superseded by SP 800-63B-4 Digital Identity Guidelines: Authentication and Lifecycle Management".
- Note: this page labels the successor "Authentication and Lifecycle Management", while the final document [S1] is titled "Authentication and Authenticator Management". The final document [S1] is the authority for the title.

---

## Topic 2. OWASP Cheat Sheets

(All six requested sheets fetched from https://cheatsheetseries.owasp.org on 2026-10-31. The pages are living documents with no version number; the fetch date is the only date I can give. A seventh sheet, Passkey Security, was added because the MFA sheet points to it.)

### [S4] Password Storage Cheat Sheet — OWASP Foundation — https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html — living document, read 2026-10-31
- "Use Argon2id with a minimum configuration of 19 MiB of memory, an iteration count of 2, and 1 degree of parallelism."
- Equivalent settings: "Use one of the following minimum configurations: m=47104 (46 MiB), t=1, p=1 (Do not use with Argon2i) m=19456 (19 MiB), t=2, p=1 (Do not use with Argon2i) m=12288 (12 MiB), t=3, p=1 m=9216 (9 MiB), t=4, p=1 m=7168 (7 MiB), t=5, p=1 These configuration settings provide an equal level of defense, and the only difference is a trade off between CPU and RAM usage."
- "Benchmark the chosen parameters on the target system; increasing parallelism is not a guarantee of slower password guessing."
- Fallbacks: "If Argon2id is not available, use scrypt with a minimum CPU/memory cost parameter of (2^17), a minimum block size of 8 (1024 bytes), and a parallelization parameter of 1. For legacy systems using bcrypt, use a work factor of 10 or more and with a password limit of 72 bytes. If FIPS-140 compliance is required, use PBKDF2 with a work factor of 600,000 or more and set with an internal hash function of HMAC-SHA-256."
- Pepper: "Consider using a pepper to provide additional defense in depth (though alone, it provides no additional secure characteristics)."

### [S5] Authentication Cheat Sheet — OWASP Foundation — https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html — living document, read 2026-10-31
- Length (cites NIST): "If MFA is enabled passwords shorter than 8 characters are considered to be weak (NIST SP800-63B). If MFA is not enabled passwords shorter than 15 characters are considered to be weak (NIST SP800-63B). Maximum password length should be at least 64 characters to allow passphrases (NIST SP800-63B)."
- Breached passwords: "Pwned Passwords is a service where passwords can be checked against previously breached passwords. [...] Alternatively, you can download the Pwned Passwords database using this mechanism to host it yourself."
- Generic responses: "an application must respond with a generic error message regardless of whether: The user ID or password was incorrect. The account does not exist. The account is locked or disabled." Purpose: "The objective is to prevent the creation of a discrepancy factor, allowing an attacker to mount a user enumeration action against the application."
- Lockout counter: "The counter of failed logins should be associated with the account itself, rather than the source IP address, in order to prevent an attacker from making login attempts from a large number of different IP addresses."
- Lockout design factors: "The number of failed attempts before the account is locked out (lockout threshold). The time period that these attempts must occur within (observation window). How long the account is locked out for (lockout duration)."
- Exponential lockout: "some applications use an exponential lockout, where the lockout duration starts as a very short period (e.g., one second), but doubles after each failed login attempt."
- Lockout as denial of service: "care must be taken to prevent it from being used to cause a denial of service by locking out other users' accounts. One way this could be performed is to allow the use of the forgotten password functionality to log in, even if the account is locked out."
- Re-authentication: "In order to mitigate CSRF and session hijacking, it's important to require the current credentials for an account before updating sensitive account information such as the user's password or email address" and "Require an authenticator already bound to the account, such as the current password or a passkey."
- OAuth 2.1 status (as of fetch): "OAuth 2.1 is an active IETF Working Group draft that consolidates OAuth 2.0 and widely adopted best practices and is intended to replace RFC 6749/6750"
- Passkeys: "WebAuthn credentials form the foundation of modern Passkeys technology. Passkeys enable users to securely log in using local user verification (such as biometrics or device PINs), often with credential synchronization across devices."

### [S6] Session Management Cheat Sheet — OWASP Foundation — https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html — living document, read 2026-10-31
- Cookie prefixes: "Use cookie name prefixes to bind cookies to security properties at the browser level (RFC 6265bis §4.1.3): __Host- — the cookie must be set with Secure, must not have a Domain attribute, and must use Path=/. Prevents subdomain forgery and HTTPS downgrade attacks. Recommended for session IDs." Example given: "Set-Cookie: __Host-SessionID=<value>; Secure; HttpOnly; SameSite=Strict; Path=/"
- SameSite: "Session cookies must explicitly set SameSite=Strict (preferred) or SameSite=Lax. Never use SameSite=None without Secure, and do not rely on the browser-default value, which varies across browsers and versions." and "Treat SameSite as defense in depth against CSRF, not as a replacement for a CSRF token."
- HttpOnly: "This session ID protection is mandatory to prevent session ID stealing through XSS attacks." Secure: "This session protection mechanism is mandatory to prevent the disclosure of the session ID through MitM (Man-in-the-Middle) attacks."
- Entropy: "Session identifiers must have at least 64 bits of entropy to prevent brute-force session guessing attacks."
- Renewal: "The session ID must be renewed or regenerated by the web application after any privilege level change within the associated user session."
- Timeouts: "Common idle timeouts ranges are 2-5 minutes for high-value applications and 15-30 minutes for low risk applications." and "If the application is intended to be used by an office worker for a full day, an appropriate absolute timeout range could be between 4 and 8 hours."
- Idle timeout is not a defence against hijack: "if the attacker is able to hijack a given session, the idle timeout does not limit the attacker's actions, as they can generate activity on the session periodically to keep the session active for longer periods of time."
- Token storage in SPAs: "Do not store authentication tokens, session IDs, JWTs, refresh tokens, or any credential in localStorage or sessionStorage. These APIs are accessible to any JavaScript executing in the origin, so a single XSS vulnerability discloses every token. Use HttpOnly; Secure; SameSite=Strict cookies (preferred) or a Backend-for-Frontend (BFF) pattern."

### [S7] Multifactor Authentication Cheat Sheet — OWASP Foundation — https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html — living document, read 2026-10-31
- Value: "MFA is by far the best defense against the majority of password-related attacks, including brute-force, credential stuffing and password spraying, with analysis by Microsoft suggesting that it would have stopped 99.9% of account compromises."
- Email as a factor: "There is some debate as to whether email constitutes a form of MFA, because if the user does not have MFA configured on their email account, it simply requires knowledge of the user's email password (which is often the same as their application password)." Cons listed include "Relies entirely on the security of the email account, which often lacks MFA."
- SMS: "Due to the risks posed by these methods, they should not be used to protect applications that hold Personally Identifiable Information (PII) or where there is financial risk."
- TOTP: "The authenticator app generates a numeric code using a configured time step; RFC 6238 recommends a default of 30 seconds. The app and server must use the same time-step value."
- OTP storage: "hashing OTPs does not provide strong offline attack resistance in the way password hashing does."
- Passkeys as MFA: "When using passkeys as MFA, require user verification and validate the returned user-verification flag on the server; a touch confirming user presence alone is not a second factor."
- Recommendations: "Require MFA for administrative or other high privileged users. Implement a secure procedure to allow users to reset their MFA."
- Step-up actions: "Changing passwords or security questions. Changing the email address associated with the account. Disabling MFA. Elevating a user session to an administrative session."
- Recovery: "Providing the user with a number of single-use recovery codes when they first setup MFA."
- Notification: "Notify the user through out-of-band channels (such as email or push notification) whenever an MFA factor is changed."

### [S8] Forgot Password Cheat Sheet — OWASP Foundation — https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html — living document, read 2026-10-31
- "Return a consistent message for both existent and non-existent accounts. Ensure that the time taken for the user response message is uniform. Use a side-channel to communicate the method to reset their password."
- Tokens: "Ensure that generated tokens or codes are: Randomly generated using a cryptographically safe algorithm. Sufficiently long to protect against brute-force attacks. Stored securely. Single use and expire after an appropriate period."
- "Do not make a change to the account until a valid token is presented, such as locking out the account."
- After reset: "Don't automatically log the user in, as this introduces additional complexity to the authentication and session handling code, and increases the likelihood of introducing vulnerabilities. Ask the user if they want to invalidate all of their existing sessions, or invalidate the sessions automatically."
- Compromise: "A password reset alone may not restore control of a compromised account. An attacker may still have an active session or may have changed recovery information or MFA methods."

### [S9] Credential Stuffing Prevention Cheat Sheet — OWASP Foundation — https://cheatsheetseries.owasp.org/cheatsheets/Credential_Stuffing_Prevention_Cheat_Sheet.html — living document, read 2026-10-31
- "Multi-factor authentication (MFA) is by far the best defense against the majority of password-related attacks, including credential stuffing and password spraying [...] As such, it should be implemented wherever possible."
- "Where it is not possible to implement MFA, there are many alternative defenses that can be used to protect against credential stuffing and password spraying. In isolation none of these are as effective as MFA, however multiple, layered defenses can provide a reasonable degree of protection."
- Risk signals listed: "A new browser/device or IP address. An unusual country or location. [...] An IP address that has tried to login to multiple accounts. A login attempt that appears to be scripted or from a bot rather than a human"

### [S10] Passkey Security Cheat Sheet — OWASP Foundation — https://cheatsheetseries.owasp.org/cheatsheets/Passkey_Security_Cheat_Sheet.html — living document, read 2026-10-31
- Synced vs device-bound: "Synced passkeys improve availability by making a credential usable on more than one device. Their security also depends on the credential provider's account protection, device enrollment, synchronization, and recovery controls. Device-bound credentials can support policies that require the key to remain on a particular authenticator, but require an availability and replacement plan."
- Policy: "Public-facing applications should generally support synced passkeys and provide users with secure options for additional authenticators and recovery. Enterprise or high-assurance applications may require managed, device-bound authenticators and attestation when the operational controls justify the reduced interoperability."
- "Use backup eligibility and backup state as risk and policy inputs where appropriate. Do not treat them as user-verification results or as proof of a specific sync provider."
- "Do not assume a synced passkey can meet every assurance level. For example, NIST does not allow syncable authenticators at Authentication Assurance Level 3 (AAL3)."
- Management: "Require recent reauthentication before adding or removing a passkey, changing recovery methods, or disabling the last strong authenticator. Allow users to register more than one authenticator."
- Recovery: "The effective security of a passkey account is limited by its weakest recovery route." and "Do not let email or SMS recovery silently bypass a stronger authentication policy for high-risk accounts."
- Challenges: "Generate cryptographically random, single-use, expiring challenges of at least 16 bytes."

---

## Topic 3. RFC 9106 (Argon2) recommended parameters

### [S11] RFC 9106, Argon2 Memory-Hard Function for Password Hashing and Proof-of-Work Applications — IRTF (CFRG) — https://www.rfc-editor.org/rfc/rfc9106.txt — September 2021, Category: Informational (not a Standards Track document; no "obsoleted by"/"updated by" entries in the RFC Editor metadata [S39])
- First option: "select Argon2id with t=1 iteration, p=4 lanes, m=2^(21) (2 GiB of RAM), 128-bit salt, and 256-bit tag size. This is the FIRST RECOMMENDED option."
- Second option: "If much less memory is available, a uniformly safe option is Argon2id with t=3 iterations, p=4 lanes, m=2^(16) (64 MiB of RAM), 128-bit salt, and 256-bit tag size. This is the SECOND RECOMMENDED option."
- Section 7.4: "The Argon2id variant with t=1 and 2 GiB memory is the FIRST RECOMMENDED option and is suggested as a default setting for all environments. This setting is secure against side-channel attacks and maximizes adversarial costs on dedicated brute-force hardware. The Argon2id variant with t=3 and 64 MiB memory is the SECOND RECOMMENDED option and is suggested as a default setting for memory-constrained environments."
- Type choice: "If you do not know the difference between the types or you consider side-channel attacks to be a viable threat, choose Argon2id."
- Salt: "16 bytes is RECOMMENDED for password hashing. The salt SHOULD be unique for each password." and "A length of 128 bits is sufficient for all applications but can be reduced to 64 bits in the case of space constraints."
- Related values from other sources, for comparison: OWASP [S4] minimum is m=19456 KiB, t=2, p=1 (about 19 MiB); the argon2-cffi library default is the RFC's SECOND option (64 MiB, t=3, p=4) [S41]. The RFC's two options and OWASP's minimums are different numbers, because they answer different questions (RFC: recommended defaults; OWASP: minimum equivalents). Both are recorded; the RFC is the older text (2021), OWASP is a living page read 2026-10-31.

---

## Topic 4. RFC 6238 (TOTP) and RFC 4226 (HOTP)

### [S12] RFC 6238, TOTP: Time-Based One-Time Password Algorithm — IETF — https://www.rfc-editor.org/rfc/rfc6238.txt — May 2011, Informational (no obsoleted-by/updated-by in [S39])
- Default step: "X represents the time step in seconds (default value X = 30 seconds) and is a system parameter."
- "We RECOMMEND a default time-step size of 30 seconds. This default value of 30 seconds is selected as a balance between security and usability."
- Verifier window: "we RECOMMEND that the validator be set with a specific limit to the number of time steps a prover can be "out of synch" before being rejected. This limit can be set both forward and backward from the calculated time step on receipt of the OTP value."
- Network delay: "We RECOMMEND that at most one time step is allowed as the network delay." and "A larger acceptable delay window would expose a larger window for attacks."
- Drift arithmetic given by the RFC: "If the time step is 30 seconds as recommended, and the validator is set to only accept two time steps backward, then the maximum elapsed time drift would be around 89 seconds"
- Replay: "Note that a prover may send the same OTP inside a given time-step window multiple times to a verifier. The verifier MUST NOT accept the second attempt of the OTP after the successful validation has been issued for the first OTP, which ensures one-time only use of an OTP."
- Keys: "R5: There MUST be a unique secret (key) for each prover." "R6: The keys SHOULD be randomly generated or derived using key derivation algorithms."

### [S13] RFC 4226, HOTP: An HMAC-Based One-Time Password Algorithm — IETF — https://www.rfc-editor.org/rfc/rfc4226.txt — December 2005, Informational (no obsoleted-by/updated-by in [S39])
- Digits: "The HOTP value must be at least a 6-digit value."
- Secret length: "The length of the shared secret MUST be at least 128 bits. This document RECOMMENDs a shared secret length of 160 bits."
- Throttling (7.3): "Truncating the HMAC-SHA-1 value to a shorter value makes a brute force attack possible. Therefore, the authentication server needs to detect and stop brute force attacks. We RECOMMEND setting a throttling parameter T, which defines the maximum number of possible attempts for One-Time Password validation." and "T SHOULD be set as low as possible, while still ensuring that usability is not significantly impacted."
- Requirement RP2: "P SHOULD NOT be vulnerable to brute force attacks. This implies that a throttling/lockout scheme is RECOMMENDED on the validation server side."
- Delay option: "Another option would be to implement a delay scheme to avoid a brute force attack."

Cross-references already quoted above: NIST requires "Verifiers SHALL accept a given OTP only once while it is valid" and a lifetime tied to drift [S1]; OWASP says RFC 6238 default of 30 seconds [S7]; pyotp's own checklist is in [S43].

---

## Topic 5. W3C Web Authentication; FIDO passkeys

### [S14] Web Authentication: An API for accessing Public Key Credentials, Level 3 — W3C — https://www.w3.org/TR/webauthn-3/ — "W3C Recommendation, 25 August 2026"
- Status line: "Web Authentication: An API for accessing Public Key Credentials Level 3 W3C Recommendation, 25 August 2026". Version link: "https://www.w3.org/TR/2026/REC-webauthn-3-20260825/". Previous versions listed: "https://www.w3.org/TR/2026/CR-webauthn-3-20260526/ https://www.w3.org/TR/2021/REC-webauthn-2-20210408/ https://www.w3.org/TR/2019/REC-webauthn-1-20190304/".
- Backup eligibility: "Backup eligibility is a credential property and is permanent for a given public key credential source. A backup eligible public key credential source is referred to as a multi-device credential whereas one that is not backup eligible is referred to as a single-device credential."
- Synced passkeys named: "provide phishing-resistant sign in using multi-device credentials (commonly referred to as synced passkeys)".
- Verification steps (7.2): "Verify that the value of C.origin is an origin expected by the Relying Party."; "Verify that the rpIdHash in authData is the SHA-256 hash of the RP ID expected by the Relying Party."; "If user verification was determined to be required, verify that the UV bit of the flags in authData is set."; "If the BE bit of the flags in authData is not set, verify that the BS bit is not set."
- Challenges (13.4.3): "In order to prevent replay attacks, the challenges MUST contain enough entropy to make guessing them infeasible. Challenges SHOULD therefore be at least 16 bytes long."
- Single-device credentials: "When the BE flag is set to 0, the credential is a single-device credential and the generating authenticator will never allow the credential to be backed up. A single-device credential is not resilient to single device loss. Relying Parties SHOULD ensure that each user account has additional authenticators registered"
- Credential loss (13.4.6): "Relying Parties SHOULD allow and encourage users to register multiple credentials to the same user account."
- Enumeration: "When verifying an AuthenticatorAssertionResponse response from the authenticator, make it indistinguishable whether verification failed because the signature is invalid or because no such user or credential is registered"
- Algorithms: "Relying Parties that wish to support a wide range of authenticators SHOULD include at least the following COSEAlgorithmIdentifier values: -8 (EdDSA) -7 (ES256) -257 (RS256)"
- Answer to "current Level and status": Level 3 is a W3C Recommendation (25 August 2026). Level 2 is "REC-webauthn-2-20210408".

### [S15] FIDO Alliance, Passkeys — FIDO Alliance — https://fidoalliance.org/passkeys/ — read 2026-10-31 (page carries no version)
- "Passkeys can be securely synced across a user's devices, or bound to a particular device (device-bound passkeys)."
- "passkeys that are synced between user's devices via a cloud service are generally referred to as "synced passkeys", and those that never leave a single device (including those on FIDO security keys or UAF apps) are referred to as "device-bound passkeys.""
- "Passkey syncing is end-to-end encrypted, and passkey providers have strong account security protections."
- Provider types: "A passkey provider can be a browser or operating system vendor where passkeys are stored and synced within the built in credential manager (such as iCloud keychain or Google password manager), or a third party provider where passkeys are stored and synced within a third party app or browser extension (such as 1Password or Dashlane)."
- Also see NIST Appendix B [S1] and OWASP Passkey sheet [S10] for how assurance rules treat synced credentials.

---

## Topic 6. OAuth standards

### [S16] RFC 9700, Best Current Practice for OAuth 2.0 Security (BCP 240) — IETF — https://www.rfc-editor.org/rfc/rfc9700.txt — January 2025; "Updates: 6749, 6750, 6819" (matches [S39])
- PKCE: "Public clients MUST use PKCE [RFC7636] to this end" and "For confidential clients, the use of PKCE [RFC7636] is RECOMMENDED". Server side: "Authorization servers MUST support PKCE [RFC7636]. If a client sends a valid PKCE code_challenge parameter in the authorization request, the authorization server MUST enforce the correct usage of code_verifier at the token endpoint."
- Redirect URIs: "When comparing client redirection URIs against pre-registered URIs, authorization servers MUST utilize exact string matching except for port numbers in localhost redirection URIs of native apps"
- Implicit and password grants: "The implicit grant (response type token) and other response types causing the authorization server to issue access tokens in the authorization response are vulnerable to access token leakage and access token replay"; "The resource owner password credentials grant [RFC6749] MUST NOT be used."
- Sender-constraining: "Authorization and resource servers SHOULD use mechanisms for sender-constraining access tokens, such as mutual TLS for OAuth 2.0 [RFC8705] or OAuth 2.0 Demonstrating Proof of Possession (DPoP) [RFC9449]"
- Refresh tokens: "Refresh tokens for public clients MUST be sender-constrained or use refresh token rotation as described in Section 4.14." Rotation defined: "Refresh token rotation: the authorization server issues a new refresh token with every access token refresh response. The previous refresh token is invalidated, but information about the relationship is retained by the authorization server." Also "Authorization servers MUST determine, based on a risk assessment, whether to issue refresh tokens to a certain client."
- Audience restriction: "access tokens SHOULD be audience-restricted to a specific resource server or, if that is not feasible, to a small set of resource servers." and "every resource server is obliged to verify, for every request, whether the access token sent with that request was meant to be used for that particular resource server. If it was not, the resource server MUST refuse to serve the respective request."
- Metadata: "It is therefore RECOMMENDED that authorization servers publish OAuth Authorization Server Metadata according to [RFC8414] and that clients make use of this Authorization Server Metadata (when available) to configure themselves."

### [S17] RFC 7636, Proof Key for Code Exchange by OAuth Public Clients — IETF — https://www.rfc-editor.org/rfc/rfc7636.txt — September 2015, Proposed Standard (no obsoleted-by/updated-by in [S39])
- Verifier format: "code_verifier = high-entropy cryptographic random STRING using the unreserved characters [A-Z] / [a-z] / [0-9] / "-" / "." / "_" / "~" from Section 2.3 of [RFC3986], with a minimum length of 43 characters and a maximum length of 128 characters."
- Method: "Clients are permitted to use "plain" only if they cannot support "S256" for some technical reason and know via out-of-band configuration that the server supports "plain"."
- "Clients MUST NOT downgrade to "plain" after trying the "S256" method. Servers that support PKCE are required to support "S256", and servers that do not support PKCE will simply ignore the unknown "code_verifier"."

### [S18] RFC 8414, OAuth 2.0 Authorization Server Metadata — IETF — https://www.rfc-editor.org/rfc/rfc8414.txt — June 2018, Proposed Standard (no obsoleted-by/updated-by in [S39])
- "By default, the well-known URI string used is "/.well-known/oauth-authorization-server"."
- Validation: "The "issuer" value returned MUST be identical to the authorization server's issuer identifier value into which the well-known URI string was inserted to create the URL used to retrieve the metadata. If these values are not identical, the data contained in the response MUST NOT be used."
- PKCE advertisement: "code_challenge_methods_supported OPTIONAL. JSON array containing a list of Proof Key for Code Exchange (PKCE) [RFC7636] code challenge methods supported by this authorization server."

### [S19] RFC 7591, OAuth 2.0 Dynamic Client Registration Protocol — IETF — https://www.rfc-editor.org/rfc/rfc7591.txt — July 2015, Proposed Standard (no obsoleted-by/updated-by in [S39])
- Open vs protected (Appendix A.1): "Authorization servers that support open registration allow registrations to be made with no initial access token. This allows all client software to register with the authorization server." and "Authorization servers that support protected registration require that an initial access token be used when making registration requests."
- Redirect URIs: "For clients that use redirect-based grant types such as "authorization_code" and "implicit", authorization servers MUST require clients to register their redirection URI values."
- Proliferation: "Since such clients often do not have long-term storage, it is possible that such clients would need to re-register every time the browser application is loaded. To avoid the resulting proliferation of dead client identifiers, an authorization server MAY decide to expire registrations for existing clients meeting certain criteria after a period of time has elapsed."
- Transport: "the authorization server MUST require the use of a transport-layer security mechanism when sending requests to the registration endpoint."

### [S20] RFC 7009, OAuth 2.0 Token Revocation — IETF — https://www.rfc-editor.org/rfc/rfc7009.txt — August 2013, Proposed Standard (no obsoleted-by/updated-by in [S39])
- "The client requests the revocation of a particular token by making an HTTP POST request to the token revocation endpoint URL. This URL MUST conform to the rules given in [RFC6749], Section 3.1. Clients MUST verify that the URL is an HTTPS URL."
- Cascade: "If the particular token is a refresh token and the authorization server supports the revocation of access tokens, then the authorization server SHOULD also invalidate all access tokens based on the same authorization grant" and "If the token passed to the request is an access token, the server MAY revoke the respective refresh token as well."
- Response: "The authorization server responds with HTTP status code 200 if the token has been revoked successfully or if the client submitted an invalid token."

### [S21] RFC 9728, OAuth 2.0 Protected Resource Metadata — IETF — https://www.rfc-editor.org/rfc/rfc9728.txt — April 2025, Proposed Standard (no obsoleted-by/updated-by in [S39])
- "By default, the well-known URI string used is /.well-known/oauth-protected-resource."
- Metadata fields: "resource REQUIRED. The protected resource's resource identifier" and "authorization_servers OPTIONAL. JSON array containing a list of OAuth authorization server issuer identifiers, as defined in [RFC8414], for authorization servers that can be used with this protected resource."
- Validation: "The resource value returned MUST be identical to the protected resource's resource identifier value into which the well-known URI path suffix was inserted to create the URL used to retrieve the metadata. If these values are not identical, the data contained in the response MUST NOT be used."
- Challenge: "If the protected resource metadata was retrieved from a URL returned by the protected resource via the WWW-Authenticate resource_metadata parameter, then the resource value returned MUST be identical to the URL that the client used to make the request to the resource server."

### [S22] RFC 8707, Resource Indicators for OAuth 2.0 — IETF — https://www.rfc-editor.org/rfc/rfc8707.txt — February 2020, Proposed Standard (no obsoleted-by/updated-by in [S39])
- "Multiple "resource" parameters MAY be used to indicate that the requested token is intended to be used at multiple resources."
- "The client SHOULD use the base URI of the API as the "resource" parameter value unless specific knowledge of the resource dictates otherwise."
- Audience: "The authorization server SHOULD audience-restrict issued access tokens to the resource(s) indicated by the "resource" parameter. Audience restrictions can be communicated in JSON Web Tokens [RFC7519] with the "aud" claim"
- Error: "invalid_target The requested resource is invalid, missing, unknown, or malformed."

### [S23] RFC 10017, OAuth 2.0 for Browser-Based Applications (the IETF draft, now published) — IETF (Parecki, De Ryck, Waite) — https://www.rfc-editor.org/rfc/rfc10017.txt — August 2026, Best Current Practice
- Status of the draft: draft-ietf-oauth-browser-based-apps has been published as RFC 10017 in August 2026 (confirmed by the Datatracker page [S24] and the RFC Editor metadata [S39]: pub_status "BEST CURRENT PRACTICE", pub_date "August 2026"). The header reads "Request for Comments: 10017 [...] BCP: 212 [...] Category: Best Current Practice [...] August 2026".
- Pattern ranking: "There are three main architectural patterns available when building browser-based applications that rely on OAuth for accessing protected resources" (BFF, token-mediating backend, browser-based OAuth client) and "The patterns in this section are presented in decreasing order of security."
- BFF duties: "The BFF interacts with the authorization server as a confidential OAuth client (as defined in Section 2.1 of [RFC6749]). 2. The BFF manages OAuth access and refresh tokens in the context of a cookie-based session, avoiding the direct exposure of any tokens to the browser-based application. 3. The BFF forwards all requests to a resource server, augmenting them with the correct access token before forwarding them to the resource server."
- Effect against malicious script: "Since tokens are only available to the BFF, there are no tokens available to extract from the browser" and "Note that the use of HttpOnly cookies prevents the attacker from directly accessing the session state, which prevents the escalation from client hijacking to session hijacking."
- Cookie rules for a BFF (6.1.3.2): "The BFF MUST enable the Secure flag for its cookies. * The BFF MUST enable the HttpOnly flag for its cookies. * The BFF SHOULD enable the SameSite=Strict flag for its cookies. * The BFF SHOULD set its cookie path to /. * The BFF SHOULD NOT set the Domain attribute for cookies. * The BFF SHOULD start the name of its cookie with a prefix indicating the cookie was set via HTTP, for example, by using the __Host-Http- prefix defined in [COOKIES]." (The normative reference [COOKIES] is the Internet-Draft draft-ietf-httpbis-layered-cookies-02, see [S60].)
- CSRF: "The BFF MUST implement a proper CSRF defense."
- Single-domain apps (7.1, heading "Single-Domain Browser-Based Applications (Not Using OAuth)"): "Too often, simple applications are made needlessly complex by using OAuth to replace the concept of session management." and "Instead of using access tokens, the application can rely on server-side cookie-based session state to keep track of the user's authentication status." The same section lists advantages of still using OAuth, including "Centralizing login and multi-factor authentication support, account management, and recovery at the OAuth server".
- Token-mediating backend (6.2): "the token-mediating backend is less secure than a BFF but still offers significant advantages over an OAuth client application running directly in the browser."
- Browser storage (8.5): "localStorage does not protect against unauthorized access from malicious JavaScript, as the attacker would be running code within the same origin, and as such, would be able to read the contents of the localStorage."
- Relation to BCP: "Many of these recommendations are derived from the Best Current Practice for OAuth 2.0 Security [RFC9700], as browser-based applications are expected to follow those recommendations as well."

### [S24] IETF Datatracker, draft-ietf-oauth-browser-based-apps — IETF — https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/ — read 2026-10-31
- "RFC 10017 part of BCP 212 [...] Document Type RFC - Best Current Practice (August 2026) Was draft-ietf-oauth-browser-based-apps (oauth WG) Authors A. Parecki , P. De Ryck , D. Waite Last updated 2026-08-21".
- Conclusion: the draft is no longer a draft; cite RFC 10017 [S23].

### [S25] IETF Datatracker, draft-ietf-oauth-v2-1 (OAuth 2.1) — IETF — https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/ — read 2026-10-31
- "Document Type Active Internet-Draft (oauth WG) Authors Dick Hardt , Aaron Parecki , Torsten Lodderstedt Last updated 2026-10-30" and the text shows "draft-ietf-oauth-v2-1-17 [...] Obsoletes: 6749, 6750".
- Note: the MCP specification still cites "draft-ietf-oauth-v2-1-13" [S27]. OAuth 2.1 is still an Internet-Draft on 2026-10-30, so it is not a standard.

---

## Topic 7. Model Context Protocol authorization

### [S26] MCP specification, Versioning — Model Context Protocol project — https://modelcontextprotocol.io/specification/versioning — read 2026-10-31
- Page header shows "Version 2026-07-28 (latest)".
- "Current: the current protocol version, which is ready for use and may continue to receive backwards compatible changes. Final: past, complete specifications that will not be changed. The current protocol version is 2026-07-28."
- Version carried per request: "Every request declares the protocol version it is using via the io.modelcontextprotocol/protocolVersion key in its _meta field [...] On Streamable HTTP, the same value is also carried in the MCP-Protocol-Version header."

### [S27] MCP specification 2026-07-28, Authorization — Model Context Protocol project — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization — version 2026-07-28 (current)
- Optional: "Authorization is OPTIONAL for MCP implementations. When supported: Implementations using an HTTP-based transport SHOULD conform to this specification."
- Standards listed: "OAuth 2.1 IETF DRAFT (draft-ietf-oauth-v2-1-13) OAuth 2.0 Bearer Token Usage (RFC6750) OAuth 2.0 Authorization Server Metadata (RFC8414) OAuth 2.0 Dynamic Client Registration Protocol (RFC7591) Resource Indicators for OAuth 2.0 (RFC8707) OAuth 2.0 Protected Resource Metadata (RFC9728) OAuth 2.0 Authorization Server Issuer Identification (RFC9207) OAuth Client ID Metadata Documents (draft-ietf-oauth-client-id-metadata-document-00) OpenID Connect Discovery 1.0 OpenID Connect Dynamic Client Registration 1.0 (OpenID Connect Registration)"
- Roles: "A protected MCP server acts as an OAuth 2.1 resource server [...] An MCP client acts as an OAuth 2.1 client [...] The implementation details of the authorization server are beyond the scope of this specification."
- Authorization server duties: "Authorization servers MUST implement OAuth 2.1 with appropriate security measures for both confidential and public clients. Authorization servers and MCP clients SHOULD support OAuth Client ID Metadata Documents (draft-ietf-oauth-client-id-metadata-document-00). Authorization servers and MCP clients MAY support the OAuth 2.0 Dynamic Client Registration Protocol (RFC7591). Note that Dynamic Client Registration is deprecated and retained for backwards compatibility with authorization servers that do not support Client ID Metadata Documents."
- Discovery: "MCP servers MUST implement OAuth 2.0 Protected Resource Metadata (RFC9728). MCP clients MUST use OAuth 2.0 Protected Resource Metadata for authorization server discovery. MCP authorization servers MUST provide at least one of the following discovery mechanisms: OAuth 2.0 Authorization Server Metadata (RFC8414) OpenID Connect Discovery 1.0 MCP clients MUST support both discovery mechanisms"
- Issuer check: "MCP authorization servers SHOULD include the iss parameter in authorization responses, including error responses, as defined in RFC9207 Section 2." and "A future revision of this specification is expected to upgrade authorization server inclusion of iss from SHOULD to MUST."
- Resource indicators: "MCP clients MUST implement Resource Indicators for OAuth 2.0 as defined in RFC 8707 to explicitly specify the target resource for which the token is being requested. The resource parameter: MUST be included in both authorization requests and token requests. MUST identify the MCP server that the client intends to use the token with. MUST use the canonical URI of the MCP server as defined in RFC 8707 Section 2." and "MCP clients MUST send this parameter regardless of whether authorization servers support it."
- Token audience: "MCP servers MUST validate that access tokens were issued specifically for them as the intended audience, according to RFC 8707 Section 2. If validation fails, servers MUST respond according to OAuth 2.1 Section 5.3 error handling requirements. Invalid or expired tokens MUST receive a HTTP 401 response." and "MCP servers MUST NOT accept or transit any other tokens."
- Bearer header: "Access tokens MUST NOT be included in the URI query string"
- Refresh tokens: "MCP Clients that desire refresh tokens: MUST keep refresh tokens confidential in transit and storage as specified in OAuth 2.1 Section 4.3 SHOULD include refresh_token in their grant_types client metadata MAY add offline_access to the scope parameter [...] MUST NOT assume refresh tokens will be issued; the AS retains discretion"
- Scope step-up: "HTTP 403 Forbidden status code (per RFC 6750 Section 3.1) WWW-Authenticate header with the Bearer scheme and additional parameters: error="insufficient_scope""

### [S28] MCP specification 2026-07-28, Authorization Server Discovery — Model Context Protocol project — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery — version 2026-07-28
- "The Protected Resource Metadata document returned by the MCP server MUST include the authorization_servers field containing at least one authorization server."
- "MCP servers MUST implement one of the following discovery mechanisms to provide authorization server location information to MCP clients: WWW-Authenticate Header: Include the resource metadata URL in the WWW-Authenticate HTTP header under resource_metadata when returning 401 Unauthorized responses, as described in RFC9728 Section 5.1. Well-Known URI: Serve metadata at a well-known URI as specified in RFC9728."
- "MCP clients MUST support both discovery mechanisms and use the resource metadata URL from the parsed WWW-Authenticate headers when present"
- Metadata endpoints clients try (with path-insertion forms): "OAuth 2.0 Authorization Server Metadata with path insertion: https://auth.example.com/.well-known/oauth-authorization-server/tenant1 OpenID Connect Discovery 1.0 with path insertion: https://auth.example.com/.well-known/openid-configuration/tenant1 OpenID Connect Discovery 1.0 path appending: https://auth.example.com/tenant1/.well-known/openid-configuration"

### [S29] MCP specification 2026-07-28, Client Registration — Model Context Protocol project — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration — version 2026-07-28
- Three methods and priority: "MCP supports three client registration mechanisms. [...] Client ID Metadata Documents: When client and server have no prior relationship (most common) Pre-registration: When client and server have an existing relationship Dynamic Client Registration: For backwards compatibility or specific requirements Clients supporting all options SHOULD use the following priority order: Use pre-registered client information for the server if the client has it available Use Client ID Metadata Documents if the Authorization Server indicates that it supports them (via client_id_metadata_document_supported in OAuth Authorization Server Metadata) Use Dynamic Client Registration as a fallback if the Authorization Server supports it (via registration_endpoint in OAuth Authorization Server Metadata) Prompt the user to enter the client information if no other option is available"
- DCR status: "Dynamic Client Registration is deprecated. New implementations should use Client ID Metadata Documents instead. This option remains available for backwards compatibility with authorization servers that do not support Client ID Metadata Documents."
- CIMD requirements for authorization servers: "SHOULD fetch metadata documents when encountering URL-formatted client_ids MUST validate that the fetched document's client_id matches the URL exactly SHOULD cache metadata respecting HTTP cache headers MUST validate redirect URIs presented in an authorization request against those in the metadata document MUST validate the document structure is valid JSON and contains required fields"
- CIMD format: "The client_id URL MUST use the "https" scheme and contain a path component, e.g. https://example.com/client.json The metadata document MUST include at least the following properties: client_id, client_name, redirect_uris"
- Advertisement: `{ "client_id_metadata_document_supported": true }` — "MCP clients SHOULD check for this capability and MAY fall back to Dynamic Client Registration or pre-registration if unavailable."
- DCR application type: "MCP clients MUST specify an appropriate application_type during Dynamic Client Registration."
- Binding: "Clients that use pre-registered credentials, or persist client credentials obtained via Dynamic Client Registration, MUST associate those credentials with the specific authorization server that issued them, keyed by the authorization server's issuer identifier."

### [S30] MCP specification 2026-07-28, Security Considerations (authorization) — Model Context Protocol project — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations — version 2026-07-28
- PKCE: "MCP clients MUST implement PKCE according to OAuth 2.1 Section 7.5.2 and MUST verify PKCE support before proceeding with authorization." "MCP clients MUST use the S256 code challenge method when technically capable" and "If code_challenge_methods_supported is absent, the authorization server does not support PKCE and MCP clients MUST refuse to proceed."
- OIDC providers: "Authorization servers providing OpenID Connect Discovery 1.0 MUST include code_challenge_methods_supported in their metadata to ensure MCP compatibility."
- Token lifetime and rotation: "Authorization servers SHOULD issue short-lived access tokens to reduce the impact of leaked tokens. For public clients, authorization servers MUST rotate refresh tokens as described in OAuth 2.1 Section 4.3.1 "Token Endpoint Extension"."
- Transport: "All authorization server endpoints MUST be served over HTTPS. All redirect URIs MUST be either localhost or use HTTPS."
- Redirects: "Authorization servers MUST validate exact redirect URIs against pre-registered values to prevent redirection attacks."
- Audience: "MCP servers MUST only accept tokens specifically intended for themselves and MUST reject tokens that do not include them in the audience claim or otherwise verify that they are the intended recipient of the token." and "The MCP server MUST NOT pass through the token it received from the MCP client."
- CIMD risks: "Authorization servers fetching metadata documents SHOULD consider Server-Side Request Forgery (SSRF) risks" and "Client ID Metadata Documents cannot prevent localhost URL impersonation by themselves. Authorization servers: SHOULD display additional warnings for localhost-only redirect URIs [...] MUST clearly display the redirect URI hostname during authorization"
- Consent: "MCP proxy servers using static client IDs MUST obtain user consent for each dynamically registered client before forwarding to third-party authorization servers"

### [S31] MCP specification 2025-11-25, Authorization — Model Context Protocol project — https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization — version 2025-11-25 (previous; now superseded by 2026-07-28)
- "Authorization servers and MCP clients SHOULD support OAuth Client ID Metadata Documents (draft-ietf-oauth-client-id-metadata-document-00). Authorization servers and MCP clients MAY support the OAuth 2.0 Dynamic Client Registration Protocol (RFC7591)."
- Disagreement with the current version: the 2025-11-25 text has no sentence calling DCR deprecated (the 2026-07-28 text [S27] adds "Note that Dynamic Client Registration is deprecated and retained for backwards compatibility"). The newer text is 2026-07-28.

### [S32] IETF Datatracker, draft-ietf-oauth-client-id-metadata-document — IETF — https://datatracker.ietf.org/doc/draft-ietf-oauth-client-id-metadata-document/ — read 2026-10-31
- "Versions: 00 01 02 Document Type Active Internet-Draft (oauth WG) Authors Aaron Parecki , Emelia Smith Last updated 2026-07-06".
- Status: still an Internet-Draft ("IESG state I-D Exists"), not an RFC. The MCP spec cites revision 00 [S27].

### [S33] Authentication for connectors (Claude.ai documentation) — Anthropic — https://claude.com/docs/connectors/building/authentication — read 2026-10-31 (page shows no version)
Which registration methods Claude's hosted clients support (claude.ai web, Desktop, mobile, Cowork):
- "Claude supports the following authentication types for remote MCP servers. TypeDescriptionAvailability oauth_dcrOAuth 2.0 with Dynamic Client Registration (RFC 7591)Supported by default oauth_cimdOAuth 2.0 with Client ID Metadata DocumentSupported by default oauth_anthropic_credsOAuth 2.0 with Anthropic-held client credentialsSupported. Enter the client in the developer portal custom_connectionCustom URL or OAuth client credentials entered at connection timeContact mcp-review@anthropic.com static_headersFixed credential (API key or bearer token) entered by an organization Owner as a request header when adding the connectorBeta, for a limited set of organizations noneNo authentication (authless server)Supported by default."
- Surfaces share a client: "claude.ai on the web, Desktop, mobile, and Cowork share this client. Claude Code doesn't use it, and instead uses its own Client ID Metadata Document and a loopback redirect".
- When CIMD is used: "Claude selects CIMD only when your authorization server metadata advertises both "client_id_metadata_document_supported": true and "none" in token_endpoint_auth_methods_supported. The second is required because Claude's CIMD client authenticates as a public client at your token endpoint. If either is missing, Claude falls back to DCR."
- DCR cost: "DCR causes Claude to register a new client on every fresh connection, which can result in very large numbers of registered clients on your authorization server. CIMD and Anthropic-held credentials avoid the registration call entirely."
- Manual client entry: "If a user leaves an optional client secret blank, Claude uses the client ID as a public client."
- Callbacks: "For the hosted Claude apps, which are claude.ai on the web, Desktop, mobile, and Cowork, register exactly this redirect URI: https://claude.ai/api/mcp/auth_callback" and "Claude Code is a native client and uses an RFC 8252 loopback redirect on an ephemeral port that varies per session".
- PKCE: "Claude includes a PKCE code_challenge with code_challenge_method=S256 on every authorization request, regardless of which registration mechanism it uses. Your authorization server must support S256 PKCE."
- Scopes and refresh: "Claude also appends offline_access when your authorization server metadata lists it in scopes_supported, to obtain a refresh token." and "Claude refreshes tokens reactively on a 401 response, and proactively up to five minutes before the stored expiry." and "Rotate refresh tokens for public-client connections. DCR and CIMD register Claude as a public client".
- Error codes: "Return RFC 6749-compliant error codes when a refresh token is no longer valid: invalid_grant, not invalid_request or a custom code".
- Discovery quirks: "A 401 is required to start sign-in, and Claude ignores a WWW-Authenticate header on a 200 response"; "Claude uses only the first entry in your metadata's authorization_servers list".
- Token endpoint: "Your /token endpoint must accept Content-Type: application/x-www-form-urlencoded per RFC 6749 section 4.1.3." Latency: "Claude waits up to 10 seconds for a response from your OAuth discovery, registration, and token endpoints, and up to 30 seconds for refresh token requests."
- Not supported: "A machine-to-machine client_credentials grant isn't supported".

### [S34] Connect Claude Code to tools via MCP (Claude Code documentation) — Anthropic — https://code.claude.com/docs/en/mcp — read 2026-10-31 (no version shown; text mentions "Claude Code v2.1.229")
- DCR: "You can use --callback-port on its own (with dynamic client registration) or together with --client-id (with pre-configured credentials)."
- CIMD: "Claude Code also supports servers that use a Client ID Metadata Document (CIMD) instead of Dynamic Client Registration, and discovers these automatically."
- Pre-configured: "Some MCP servers don't support automatic OAuth setup via Dynamic Client Registration. If you see an error like "Incompatible auth server: does not support dynamic client registration," the server requires pre-configured credentials." Flags shown: "--client-id", "--client-secret", "--callback-port"; JSON form: `"oauth":{"clientId":"your-client-id","callbackPort":8080}`.
- Callback port: "By default, Claude Code picks a random available port for the OAuth callback."
- Discovery override: the page has a section "Override OAuth metadata discovery" ("Set authServerMetadataUrl when the MCP server's standard endpoints error").
- Scope: this page covers Claude Code only. For claude.ai and Claude Desktop the official text is [S33].

---

## Topic 8. Cookies: RFC 6265 and RFC 6265bis

### [S35] RFC 6265, HTTP State Management Mechanism — IETF — https://www.rfc-editor.org/rfc/rfc6265.txt — April 2011, Proposed Standard; still current per the RFC Editor metadata (obsoleted_by empty, obsoletes RFC 2965) [S39]
- Secure: "The Secure attribute limits the scope of the cookie to "secure" channels (where "secure" is defined by the user agent). When a cookie has the Secure attribute, the user agent will include the cookie in an HTTP request only if the request is transmitted over a secure channel"
- HttpOnly: "The HttpOnly attribute limits the scope of the cookie to HTTP requests. In particular, the attribute instructs the user agent to omit the cookie when providing access to cookies via "non-HTTP" APIs (such as a web browser API that exposes cookies to scripts)."
- Not in RFC 6265: SameSite and the `__Host-` prefix (they are defined in the bis draft below).

### [S36] draft-ietf-httpbis-rfc6265bis-22, Cookies: HTTP State Management Mechanism — IETF HTTPbis WG (Bingler, West, Wilander) — https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.txt — 1 December 2025 (revision 22), "Obsoletes: 6265 (if approved)"
- `__Host-`: "If a cookie's name begins with a case-sensitive match for the string __Host-, then the cookie will have been set with a Secure attribute, a Path attribute with a value of /, and no Domain attribute. This combination yields a cookie that hews as closely as a cookie can to treating the origin as a security boundary."
- Accepted and rejected examples: "Set-Cookie: __Host-SID=12345; Secure; Path=/" is accepted from a secure origin; "Set-Cookie: __Host-SID=12345; Secure; Domain=site.example; Path=/" is rejected.
- `__Secure-`: "If a cookie's name begins with a case-sensitive match for the string __Secure-, then the cookie will have been set with a Secure attribute."
- SameSite grammar: "samesite-value = "Strict" / "Lax" / "None"". Default handling: in the storage algorithm, when no SameSite attribute with a value of "Strict", "Lax" or "None" is present, "set the cookie's same-site-flag to "Default"". For an unrecognised value the text says "the attribute's value will be subject to a default enforcement mode that is equivalent to "Lax"". It also says "user agents MAY apply "Lax-allowing-unsafe" enforcement only to cookies that did not explicitly specify a SameSite attribute"
- Prefix matching nuance inside the draft: the prefix definition says "a case-sensitive match for the string __Host-", while the storage algorithm step says "If the cookie-name begins with a case-insensitive match for the string "__Host-", abort this algorithm and ignore the cookie entirely unless the cookie meets all the following criteria: 1. The cookie's secure-only-flag is true. [...] 2. The cookie's host-only-flag is true. 3. The cookie-attribute-list contains an attribute with an attribute-name of "Path", and the cookie's path is /."".
- Guidance: "Notice that the server uses the Secure and HttpOnly attributes to provide additional security protections for the more sensitive session identifier".

### [S37] IETF Datatracker, draft-ietf-httpbis-rfc6265bis — IETF — https://datatracker.ietf.org/doc/draft-ietf-httpbis-rfc6265bis/ — read 2026-10-31
- "Document Type Active Internet-Draft (httpbis WG) Authors Steven Bingler , Mike West , John Wilander Last updated 2026-10-19 (Latest revision 2025-12-01)".
- "IESG state RFC Ed Queue [...] RFC Editor status blocked: Final Approval Pending" (so approved, in the RFC Editor queue, but not yet an RFC on 2026-10-31).
- Conclusion: RFC 6265bis is not yet published as an RFC. RFC 6265 [S35] is still the RFC of record.

### [S60] draft-ietf-httpbis-layered-cookies-02, Cookies: HTTP State Management Mechanism — IETF HTTPbis WG (van Kesteren, Hofmann) — https://www.ietf.org/archive/id/draft-ietf-httpbis-layered-cookies-02.txt — 21 May 2026, "Obsoletes: [6265] (if approved)", Expires 22 November 2026
- Datatracker (https://datatracker.ietf.org/doc/draft-ietf-httpbis-layered-cookies/): "Active Internet-Draft (httpbis WG) [...] Last updated 2026-05-21".
- Defines an additional prefix: "If a cookie's name begins with a case-sensitive match for the string __Host-Http-, then the cookie will have been set with a Secure attribute, an HttpOnly attribute, a Path attribute with a value of /, and no Domain attribute."
- Note: I searched the rfc6265bis-22 text [S36] for "__Host-Http-" and found no match. RFC 10017 [S23] cites this layered-cookies draft for that prefix. Two drafts that would each obsolete RFC 6265 exist as of the fetch date.

---

## Topic 9. JWT best current practice

### [S38] RFC 8725, JSON Web Token Best Current Practices (BCP 225) — IETF — https://www.rfc-editor.org/rfc/rfc8725.txt — February 2020; "Updates: 7519"; no obsoleted-by [S39]
- Algorithm pinning (3.1): "Libraries MUST enable the caller to specify a supported set of algorithms and MUST NOT use any other algorithms when performing cryptographic operations. The library MUST ensure that the "alg" or "enc" header specifies the same algorithm that is used for the cryptographic operation. Moreover, each key MUST be used with exactly one algorithm, and this MUST be checked when the cryptographic operation is performed."
- "none": "JWT libraries SHOULD NOT generate JWTs using "none" unless explicitly requested to do so by the caller. Similarly, JWT libraries SHOULD NOT consume JWTs using "none" unless explicitly requested by the caller."
- Validate everything (3.3): "All cryptographic operations used in the JWT MUST be validated and the entire JWT MUST be rejected if any of them fail to validate."
- Issuer (3.8): "When a JWT contains an "iss" (issuer) claim, the application MUST validate that the cryptographic keys used for the cryptographic operations in the JWT belong to the issuer. If they do not, the application MUST reject the JWT."
- Audience (3.9): "If the same issuer can issue JWTs that are intended for use by more than one relying party or application, the JWT MUST contain an "aud" (audience) claim that can be used to determine whether the JWT is being used by an intended party or was substituted by an attacker at an unintended party. In such cases, the relying party or application MUST validate the audience value, and if the audience value is not present or not associated with the recipient, it MUST reject the JWT."
- Typing (3.11): "Sometimes, one kind of JWT can be confused for another. If a particular kind of JWT is subject to such confusion, that JWT can include an explicit JWT type value, and the validation rules can specify checking the type."
- Different kinds (3.12): "If more than one kind of JWT can be issued by the same issuer, the validation rules for those JWTs MUST be written such that they are mutually exclusive, rejecting JWTs of the wrong kind."
- Keys: "human-memorizable passwords MUST NOT be directly used as the key to a keyed-MAC algorithm such as "HS256"."
- Cross-reference: RFC 9700 [S16] and MCP [S27, S30] require audience restriction of access tokens.

### [S39] RFC Editor machine-readable metadata for the RFCs above — RFC Editor — https://www.rfc-editor.org/rfc/rfcNNNN.json (fetched for 9106, 6238, 4226, 9700, 7636, 8414, 7591, 7009, 9728, 8707, 6265, 8725, 10017) — read 2026-10-31
- Used only for status checks. Every one of the 13 returned `"obsoleted_by": []` and `"updated_by": []`. Recorded values: RFC 9700 `"pub_status": "BEST CURRENT PRACTICE"`, `"pub_date": "January 2025"`, `"updates": ["RFC6749", "RFC6750", "RFC6819"]`; RFC 10017 `"pub_status": "BEST CURRENT PRACTICE"`, `"pub_date": "August 2026"`; RFC 6265 `"obsoletes": ["RFC2965"]`; RFC 8725 `"updates": ["RFC7519"]`; RFC 9728 `"pub_date": "April 2025"`; RFC 9106 `"pub_status": "INFORMATIONAL"`.

---

## Topic 10. Python and JavaScript libraries

Release dates below are PyPI/npm upload timestamps (UTC) read from the registries' JSON APIs on 2026-10-31.

### [S40] argon2-cffi on PyPI — PyPI — https://pypi.org/project/argon2-cffi/ (JSON: https://pypi.org/pypi/argon2-cffi/json)
- Latest: 25.1.0, uploaded 2025-06-03T06:55:30Z. Requires Python ">=3.8"; license MIT; classifier "Development Status :: 5 - Production/Stable".
- Release history (last four): 21.3.0 (2021-12-11), 23.1.0 (2023-08-15), 25.1.0 (2025-06-03). Cadence is roughly one release every one to two years.
- Maintenance status (from [S41] changelog): the repository's CHANGELOG has an "Unreleased" section with "Official support for Python 3.15. No code changes were necessary." and "Python 3.8 and 3.9 are not supported anymore.", so work continues on main after the last release. Repository-level activity (archived flag, last commit date): UNVERIFIED (GitHub API not reachable).

### [S41] argon2-cffi documentation, changelog and source — Hynek Schlawack (maintainer) — https://argon2-cffi.readthedocs.io/en/stable/parameters.html ; https://argon2-cffi.readthedocs.io/en/stable/api.html ; https://raw.githubusercontent.com/hynek/argon2-cffi/main/CHANGELOG.md ; https://raw.githubusercontent.com/hynek/argon2-cffi/main/src/argon2/profiles.py — read 2026-10-31
- RFC profiles: "As of September 2021, we have the official Internet standard RFC 9106 to help us with it. It comes with two recommendations in section 4, that (as of argon2-cffi 21.2.0) you can load directly from the argon2.profiles module: argon2.profiles.RFC_9106_HIGH_MEMORY (called "FIRST RECOMMENDED") and argon2.profiles.RFC_9106_LOW_MEMORY ("SECOND RECOMMENDED") into argon2.PasswordHasher.from_parameters()."
- Note: the docs call RFC 9106 an "official Internet standard"; the RFC itself says it is "not an Internet Standards Track specification" ([S11], Category: Informational).
- Default: "argon2.profiles.RFC_9106_LOW_MEMORY [...] Called "SECOND RECOMMENDED option" by RFC 9106. The main difference is that it only takes 64 MiB of RAM. The values from this profile are the default parameters used by argon2.PasswordHasher."
- Source values (profiles.py): `RFC_9106_LOW_MEMORY ... time_cost=3, memory_cost=65536,  # 64 MiB, parallelism=4`, `salt_len=16`, `hash_len=32`; `RFC_9106_HIGH_MEMORY ... time_cost=1, memory_cost=2097152,  # 2 GiB, parallelism=4`.
- Rehash: "Whenever your Argon2 parameters – or argon2-cffi's defaults! – change, you should rehash your passwords at the next opportunity. The common approach is to do that whenever a user logs in, since that should be the only time when you have access to the cleartext password." (API: `check_needs_rehash(hash)`).
- Timing: "One recommendation for concurrent user logins is to keep it under 0.5 ms. The RFC used to recommend under 500 ms. The truth is somewhere between those two values: more is more secure, less is a better user experience. argon2-cffi's current defaults land with ~50ms somewhere in the middle, but the actual time depends on your hardware." (The page prints "0.5 ms"; I copied it as shown and did not correct it.)

### [S42] PyOTP on PyPI — PyPI — https://pypi.org/project/pyotp/ (JSON: https://pypi.org/pypi/pyotp/json)
- Latest: 2.10.0, uploaded 2026-06-14T03:48:47Z. Requires Python ">=3.8"; license MIT; classifier "Development Status :: 5 - Production/Stable".
- Release history (last four): 2.7.0 (2022-09-11), 2.8.0 (2022-12-14), 2.9.0 (2023-07-27), 2.10.0 (2026-06-14). A gap of almost three years between 2.9.0 and 2.10.0.

### [S43] PyOTP repository files (README, Changes.rst, totp.py) and the 2.10.0 wheel — PyAuth project — https://raw.githubusercontent.com/pyauth/pyotp/develop/README.rst ; https://raw.githubusercontent.com/pyauth/pyotp/develop/Changes.rst ; https://raw.githubusercontent.com/pyauth/pyotp/develop/src/pyotp/totp.py ; wheel https://files.pythonhosted.org/packages/65/33/7b83bde70eddaaaaef487751a9c3a5cc0c0be54620ded0e120ebdc401ff9/pyotp-2.10.0-py3-none-any.whl — read 2026-10-31
- Changelog head: "Changes for v2.10.0 (2026-06-13)".
- README checklist: "Implementers should read and follow the HOTP security requirements and TOTP security considerations sections of the relevant RFCs. At minimum, application implementers should follow this checklist: - Ensure transport confidentiality by using HTTPS - Ensure HOTP/TOTP secret confidentiality by storing secrets in a controlled access database - Deny replay attacks by rejecting one-time passwords that have been used by the client (this requires storing the most recently authenticated timestamp, OTP, or hash of the OTP in your database, and rejecting the OTP when a match is seen) - Throttle (rate limit) brute-force attacks against your application's login functionality (see RFC 4226, section 7.3)"
- Window: `def verify(self, otp: str, for_time: Optional[datetime.datetime] = None, valid_window: int = 0) -> bool:` with `:param valid_window: extends the validity to this many counter ticks before and after the current one`; comparison uses `utils.strings_equal(...)`.
- Replay helper: the `develop` branch `totp.py` has `def verify_and_get_timecode(...)` ("This is useful when callers need to prevent TOTP reuse. An application can store the timecode from the last successful authentication and reject future OTPs whose matching timecode is not greater than the stored one."). I downloaded the released 2.10.0 wheel and listed the functions in its `pyotp/totp.py`: `['__init__', 'at', 'now', 'verify', 'provisioning_uri', 'timecode']`. So `verify_and_get_timecode` is on the development branch but is NOT in the 2.10.0 release.
- Library does not itself store state, so replay rejection is the caller's job (README checklist above).

### [S44] webauthn (py_webauthn) on PyPI — PyPI — https://pypi.org/project/webauthn/ (JSON: https://pypi.org/pypi/webauthn/json)
- Latest: 3.0.1, uploaded 2026-09-25T16:18:36Z. Requires Python ">=3.10"; license BSD-3-Clause; classifier "Development Status :: 5 - Production/Stable"; project URL "Homepage": https://github.com/duo-labs/py_webauthn.
- Release history (last four): 2.7.1 (2026-02-11), 2.8.0 (2026-06-13), 3.0.0 (2026-06-29), 3.0.1 (2026-09-25). Four releases in eight months.

### [S45] py_webauthn repository files (README, CHANGELOG, source) — Duo Labs — https://raw.githubusercontent.com/duo-labs/py_webauthn/master/README.md ; https://raw.githubusercontent.com/duo-labs/py_webauthn/master/CHANGELOG.md ; https://raw.githubusercontent.com/duo-labs/py_webauthn/master/webauthn/authentication/verify_authentication_response.py ; https://raw.githubusercontent.com/duo-labs/py_webauthn/master/webauthn/helpers/structs.py — read 2026-10-31
- Scope: "A Python3 implementation of the server-side of the WebAuthn API focused on making it easy to leverage the power of WebAuthn." Requirements: "Python 3.10 and up". Public functions: `generate_registration_options()`, `verify_registration_response()`, `generate_authentication_options()`, `verify_authentication_response()`.
- Note: the README links to the Level 2 spec ("https://www.w3.org/TR/webauthn-2/") even though Level 3 became a W3C Recommendation on 25 August 2026 [S14].
- Changelog v3.0.1: "`verify_registration_response()` now rejects responses with attestation statement formats that are not strings". v3.0.0: "`verify_registration_response()` and `verify_authentication_response()` now support use of PQC ML-DSA-44, ML-DSA-65, and ML-DSA-87 algorithms for credential public key signature verification" and "Responses containing malformed CBOR data with duplicate keys will now be rejected".
- Source: `verify_authentication_response(...)` takes `expected_rp_id`, `expected_origin`, `credential_current_sign_count`, `require_user_verification: bool = False` and returns a result with `new_sign_count`. `structs.py` defines `class CredentialDeviceType(str, Enum)` with `SINGLE_DEVICE = "single_device"` and `MULTI_DEVICE = "multi_device"` ("MULTI_DEVICE: A credential that can be used from multiple devices (e.g. passkeys)").
- Maintenance status: frequent releases in 2026 (see [S44]). Repository-level activity: UNVERIFIED.

### [S46] joserfc on PyPI — PyPI — https://pypi.org/project/joserfc/ (JSON: https://pypi.org/pypi/joserfc/json)
- Latest: 1.7.7, uploaded 2026-10-19T00:30:28Z. Requires Python ">=3.10"; license BSD-3-Clause; classifier "Development Status :: 5 - Production/Stable". Source link: https://github.com/authlib/joserfc.
- Release history (last four): 1.7.4 (2026-07-19), 1.7.5 (2026-08-29), 1.7.6 (2026-10-18), 1.7.7 (2026-10-19). Active.

### [S47] joserfc documentation and README — Authlib project — https://jose.authlib.org/en/dev/guide/registry/ ; https://jose.authlib.org/en/dev/guide/jwt/ ; https://jose.authlib.org/en/dev/security/ ; https://raw.githubusercontent.com/authlib/joserfc/main/README.md — read 2026-10-31
- Scope: "joserfc is a Python library that provides a comprehensive implementation of several essential JSON Object Signing and Encryption (JOSE) standards, including JWS (JSON Web Signature), JWE (JSON Web Encryption), JWK (JSON Web Key), JWA (JSON Web Algorithms), and JWT (JSON Web Tokens). It is derived from Authlib, but features a redesigned API specific to JOSE functionality."
- Algorithm allow-list: "The registry is specifically designed to store supported algorithms, allowed algorithms, registered header parameters, and provides methods to validate algorithms and headers." and "By default, it enforces the usage of recommended algorithms, ensuring a higher level of security." Example: `registry = JWSRegistry(algorithms=["RS256", "ES256"])`.
- Claim checks: `JWTClaimsRegistry(aud={"essential": True})` raises a missing-claim error when `aud` is absent; `iss={"essential": True, "value": "https://authlib.org"}` is shown for issuer checks.
- Security page lists past CVEs: "CVE-2025-65015 Affected versions: 1.3.3, 1.3.4, 1.4.0, 1.4.1 Fixed versions: 1.3.5, 1.4.2" and "CVE-2024-37568 Fixed versions: 0.11.0". Reporting: "do not submit a public issue or patch. Instead, please report it privately through the GitHub Security tab."
- Docs URL note: https://jose.authlib.org/en/latest/... returned HTTP 404; the working path is /en/dev/.

### [S48] Authlib on PyPI — PyPI — https://pypi.org/project/Authlib/ (JSON: https://pypi.org/pypi/authlib/json)
- Latest: 1.9.1, uploaded 2026-10-14T14:17:50Z. Requires Python ">=3.10"; license BSD-3-Clause; classifier "Development Status :: 5 - Production/Stable".
- Release history (last four): 1.7.2 (2026-05-06), 1.8.0 (2026-08-30), 1.9.0 (2026-10-14), 1.9.1 (2026-10-14). Active.

### [S49] Authlib documentation and README — Authlib project — https://docs.authlib.org/en/latest/oauth2/index.html ; https://raw.githubusercontent.com/authlib/authlib/main/README.md — read 2026-10-31
- Scope: "The ultimate Python library in building OAuth and OpenID Connect servers. JWS, JWK, JWA, JWT are included. Authlib is compatible with Python3.10+."
- Deprecation: "Authlib will deprecate `authlib.jose` module, please read: Migrating from `authlib.jose` to `joserfc`".
- OAuth 2.0 specification pages listed in the docs: "RFC6749 [...] RFC6750 [...] RFC7009: OAuth 2.0 Token Revocation RFC7523 [...] RFC7591: OAuth 2.0 Dynamic Client Registration Protocol RFC7592: OAuth 2.0 Dynamic Client Registration Management Protocol RFC7636: Proof Key for Code Exchange by OAuth Public Clients RFC7662: OAuth 2.0 Token Introspection RFC8414: OAuth 2.0 Authorization Server Metadata RFC8628: OAuth 2.0 Device Authorization Grant RFC9068: JSON Web Token (JWT) Profile for OAuth 2.0 Access Tokens RFC9101 [...] RFC9207: OAuth 2.0 Authorization Server Issuer Identification RFC9728: OAuth 2.0 Protected Resource Metadata". The table of contents I fetched has no page for RFC 8707 and none for Client ID Metadata Documents.
- Server integrations in the docs' navigation: under "Authorization Server", only "Flask Integration" and "Django Integration" appear; "Starlette Integration" and "FastAPI Integration" appear under "Web Clients". (Read from the navigation text of the docs index; I did not test code.)

### [S50] @simplewebauthn/browser on npm — npm registry — https://www.npmjs.com/package/@simplewebauthn/browser (JSON: https://registry.npmjs.org/@simplewebauthn/browser)
- `dist-tags: {'latest': '14.0.0'}`, published 2026-09-02T05:49:26Z; license MIT; repository "git+https://github.com/MasterKale/SimpleWebAuthn.git" (directory packages/browser); no `deprecated` field.
- Release history (last five): 13.1.2 (2025-07-05), 13.2.0 (2025-09-15), 13.2.2 (2025-10-04), 13.3.0 (2026-03-10), 14.0.0 (2026-09-02).

### [S51] SimpleWebAuthn repository README and CHANGELOG — Matthew Miller (MasterKale) — https://raw.githubusercontent.com/MasterKale/SimpleWebAuthn/master/README.md ; https://raw.githubusercontent.com/MasterKale/SimpleWebAuthn/master/CHANGELOG.md — read 2026-10-31
- README: "This project features two complimentary libraries to help reduce the amount of work needed to incorporate WebAuthn into a website. The following packages are maintained here: @simplewebauthn/server @simplewebauthn/browser".
- v14.0.0 changelog: "**[browser]** The new `sendSignal()` method offers a simple way to invoke the WebAuthn Signal APIs." and "**[browser]** The new `browserSupportsPasskeys()` helper method performs more robust WebAuthn support detection by also checking for typical passkey-related WebAuthn features." and, as a breaking change, "the minimum supported version of Node has been raised to Node LTS 22.x and higher, and Deno v2.4.x and higher."
- The same release gives the server package ML-DSA support: "The PQC ML-DSA-44, ML-DSA-65, and ML-DSA-87 signature algorithms are now supported as passkey public key algorithms when the runtime supports them." The server package is JavaScript; it is mentioned only because the browser package pairs with it. The Python server option is [S44]; whether the browser package works with py_webauthn's JSON option format: UNVERIFIED (not tested; the py_webauthn README says "JSON is the preferred data type for transmitting registration and authentication options from the server to the webpage").
- Maintenance status: active; repository-level facts UNVERIFIED.

---

## Topic 11. Have I Been Pwned: Pwned Passwords

### [S52] Have I Been Pwned API v3 documentation (Pwned Passwords section) — Have I Been Pwned (Troy Hunt) — https://haveibeenpwned.com/API/v3 — read 2026-10-31 (page footer "© 2026 Have I Been Pwned®")
- Cost and auth: "The Pwned Passwords API is freely accessible without the need for a subscription and API key."
- Data format: "Each password is stored as both a SHA-1 and an NTLM hash of a UTF-8 encoded password. The downloadable source data delimits the hash and the password count with a colon (:) and each line with a CRLF."
- Offline: "The data is queryable online via the Pwned Passwords webpage, accessible via the API or downloadable as an entire corpus of data that can be queried offline."
- Range query: "GET https://api.pwnedpasswords.com/range/{first 5 SHA-1 chars}"; "This allows the first 5 characters of either a SHA-1 or an NTLM hash (not case-sensitive) to be passed to the API".
- Response: "When a password hash with the same first 5 characters is found in the Pwned Passwords repository, the API will respond with an HTTP 200 and include the suffix of every hash beginning with the specified prefix, followed by a count of how many times it appears in the data set. The API consumer can then search the results of the response for the presence of their source hash and if not found, the password does not exist in the data set."
- Size: "A range search typically returns approximately 800 hash suffixes" and "There are 1,048,576 different hash prefixes between 00000 and FFFFF (16^5) and every single one will return HTTP 200; there is no circumstance in which the API should return HTTP 404."
- Padding: "Padding is enabled by a request header and ensures that all responses contain between 800 and 1,000 results regardless of the number of hash suffixes returned by the service." Header: "Add-Padding: true"; "Padded entries always have a password count of 0 and can be discarded once received."
- Incremental search warning: the page has a section "Incremental searching" (searching on each keystroke) which it describes as potentially giving a third party information; I copied only its first sentence: "Incremental searching involves performing a search for the password as each character is typed by the user".
- Offline download: "all password hashes are also available for download as either SHA-1 or NTLM so they can be used without the API dependency. The Pwned Passwords Downloader is freely available from GitHub and enables the API to be queried for every single hash prefix and the results saved offline." The same page says the API route is preferred: "Pwned Passwords works best when directly querying the k-anonymity API; it's fast, always up to date and enables anonymous queries without disclosing the password being searched for."
- Licence for Pwned Passwords: "In order to help maximise adoption, there is no licencing or attribution requirements on the Pwned Passwords API, although it is welcomed if you would like to include it." (The CC BY 4.0 licence on that page applies to the breach and paste APIs.)
- HTTPS: "All API endpoints must be invoked over HTTPS." "Only TLS versions 1.2 and 1.3 are supported".
- NIST link: the page says "If you're implementing compromised-password screening to meet regulatory or security guidance, see how Have I Been Pwned satisfies NIST SP 800-63B's password requirement."

### [S53] Pwned Passwords landing page — Have I Been Pwned — https://haveibeenpwned.com/Passwords — read 2026-10-31
- "The best way to get the most up-to-date passwords is via the API. Alternatively, use the free and open source Pwned Passwords downloader to take the entire corpus offline and run it yourself."
- "Pwned Passwords uses a model called k-anonymity to let you check whether a password has been seen before without ever sending the full password or its complete hash to the service. Instead, your password is hashed locally and only the first 5 characters of the SHA-1 hash are sent to the API. The service returns a list of matching suffixes, and the full comparison happens on your side so the password itself remains private."
- Scale: "Our password checking service handles over 18 billion requests monthly, delivered through Cloudflare's global network".

### [S54] HaveIBeenPwned/PwnedPasswordsDownloader README — Have I Been Pwned (GitHub) — https://raw.githubusercontent.com/HaveIBeenPwned/PwnedPasswordsDownloader/main/README.md — read 2026-10-31
- "`haveibeenpwned-downloader` is a dotnet tool to download all Pwned Passwords hash ranges and save them offline so they can be used without a dependency on the k-anonymity API."
- Integrity: "Every downloaded hash range is verified against the `Content-MD5` response header supplied by the Pwned Passwords API before it is accepted."
- Incremental refresh: "On later runs, the downloader sends the saved ETag with `If-None-Match`; ranges that have not changed are retained locally without downloading their content again."
- Prerequisite: "Install the .NET 10 SDK or later to install the tool."
- Not found: a single pre-built downloadable file hosted by HIBP itself. The official offline route documented on these pages is to run the downloader against the API. (The README mentions an alternative cURL approach in a GitHub issue.)

---

## Topic 12. Keycloak defaults (to match or improve)

### [S55] Keycloak Server Administration Guide, version 26.8.1 (page header "Version 26.8.1") — Keycloak project — https://www.keycloak.org/docs/latest/server_admin/index.html — read 2026-10-31
Brute-force detection:
- Off by default: "Brute force detection is disabled by default. Enable this feature to protect against brute force attacks."
- Scope: "Keycloak applies it only to password, OTP and recovery codes."
- User-visible behaviour: "When a user is locked and attempts to log in, Keycloak displays the default Invalid username or password error message. This message is the same error message as the message displayed for an invalid username or invalid password to ensure the attacker is unaware the account is disabled."
- Permanent lockout parameters (defaults in the table): "Max Login Failures The maximum number of login failures. 30 failures Quick Login Check Milliseconds The minimum time between login attempts. 1000 milliseconds Minimum Quick Login Wait The minimum time the user is disabled when login attempts are quicker than Quick Login Check Milliseconds. 1 minute"
- Temporary lockout parameters: "Max Login Failures [...] 30 failures [...] Multiple Wait Increment The time added to the time a user is temporarily disabled when the user's login attempts exceed Max Login Failures. 1 minute Max Wait The maximum time a user is temporarily disabled. 15 minutes Failure Reset Time The time when the failure count resets. The timer runs from the last failed login. [...] 12 hours Quick Login Check Milliseconds [...] 1000 milliseconds Minimum Quick Login Wait [...] 1 minute"
- Wait formula: "The By multiple strategy uses the following formula to calculate wait time: Wait Increment in Seconds * (count / Max Login Failures). The division is an integer division rounded down"
Token and session settings:
- The guide describes the Tokens tab fields but lists no default value for them: "Access Token Lifespan When Keycloak creates an OIDC access token, this value controls the lifetime of the token." The access-token default comes from source code, [S57]. The session-tab table likewise names "SSO Session Idle" and "SSO Session Max" without numbers; defaults in [S57].
- Refresh-token reuse: "Revoke Refresh Token When Enabled, Keycloak revokes refresh tokens and issues another token that the client must use." (the guide does not state the default for this switch).
Passwords and OTP:
- Hashing: "argon2 Argon2 (default for non-FIPS deployments) pbkdf2-sha512 PBKDF2 with SHA512 (default for FIPS deployments)" and "It is highly recommended to use Argon2 when possible as it has significantly less CPU requirements compared to PBKDF2, while at the same time being more secure." Default iterations table: "argon2 5 pbkdf2-sha512 210,000 pbkdf2-sha256 600,000 pbkdf2 1,300,000".
- OTP policy: "OTP hash algorithm The default algorithm is SHA1." and "Look around window [...] The default value of 1 is adequate. For example, if the time interval for a token is 30 seconds, the default value of 1 means it will accept valid tokens in the 90-second window" and "Reusable code Determine whether OTP tokens can be reused in the authentication process or user needs to wait for the next token. Users cannot reuse those tokens by default".
- Features list: "Two-factor Authentication - Support for passkey, recovery codes and TOTP/HOTP via Google Authenticator or FreeOTP."

### [S56] Keycloak source, RealmManager.java at tag 26.8.1 — Keycloak project (GitHub) — https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/services/managers/RealmManager.java — read 2026-10-31 (a second read of the `main` branch file is described below)
- Confirmed on tag 26.8.1, in `setupRealmDefaults`: `realm.setBruteForceProtected(false); // default settings off for now todo set it on`; `realm.setMaxFailureWaitSeconds(900);` `realm.setMinimumQuickLoginWaitSeconds(60);` `realm.setWaitIncrementSeconds(60);` `realm.setQuickLoginCheckMilliSeconds(1000);` `realm.setMaxDeltaTimeSeconds(60 * 60 * 12); // 12 hours` `realm.setFailureFactor(30);`
- Seen on `main` only (not grepped on the tag): `realm.setPermanentLockout(false);` `realm.setBruteForceStrategy(RealmRepresentation.BruteForceStrategy.MULTIPLE);` `realm.setSslRequired(SslRequired.EXTERNAL);` `realm.setLoginWithEmailAllowed(true);`
- These agree with the Administration Guide table [S55].

### [S57] Keycloak source, Constants.java at tag 26.8.1 — Keycloak project (GitHub) — https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/server-spi-private/src/main/java/org/keycloak/models/Constants.java — read 2026-10-31
- `public static final int DEFAULT_ACCESS_TOKEN_LIFESPAN = 300;` (seconds, so 5 minutes)
- `public static final int DEFAULT_SESSION_IDLE_TIMEOUT = 1800; // 30 minutes`
- `public static final int DEFAULT_SESSION_MAX_LIFESPAN = 36000; // 10 hours`
- `public static final int DEFAULT_ACCESS_CODE_LIFESPAN = 60;`

### [S58] Keycloak source, OTPPolicy.java at tag 26.8.1 — Keycloak project (GitHub) — https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/server-spi/src/main/java/org/keycloak/models/OTPPolicy.java — read 2026-10-31
- `public static OTPPolicy DEFAULT_POLICY = new OTPPolicy(OTPCredentialModel.TOTP, HmacOTP.HMAC_SHA1, 0, 6, 1, 30);` (arguments: type, algorithm, initial counter, digits, look-ahead window, period) and `public static final boolean DEFAULT_IS_REUSABLE = false;`

### [S59] Keycloak source, DefaultExportImportManager.java (main) — Keycloak project (GitHub) — https://raw.githubusercontent.com/keycloak/keycloak/main/model/storage-private/src/main/java/org/keycloak/storage/datastore/DefaultExportImportManager.java — read 2026-10-31
- Shows where the constants above are applied to a new realm: `if (rep.getAccessTokenLifespan() != null) newRealm.setAccessTokenLifespan(rep.getAccessTokenLifespan()); else newRealm.setAccessTokenLifespan(DEFAULT_ACCESS_TOKEN_LIFESPAN);` and the same pattern for `setSsoSessionIdleTimeout(DEFAULT_SESSION_IDLE_TIMEOUT)` and `setSsoSessionMaxLifespan(DEFAULT_SESSION_MAX_LIFESPAN)`. I read this file on `main`, not on the 26.8.1 tag.

---

## Version notes and disagreements between sources

1. NIST title: the superseded-notice page [S3] names the successor "Authentication and Lifecycle Management"; the final PDF [S1] is titled "Authentication and Authenticator Management". [S1] is authoritative; July 2025 is the date, 07/31/25 the final history entry [S2].
2. MCP and Dynamic Client Registration: 2025-11-25 [S31] makes DCR a MAY; 2026-07-28 [S27, S29] adds that DCR "is deprecated". The newer one is 2026-07-28 and is marked current by [S26]. Anthropic's connector docs [S33] still list `oauth_dcr` as "Supported by default" and say Claude falls back to DCR unless the authorization server advertises both CIMD values.
3. Browser-based apps: the topic text asked for the IETF draft; it is now RFC 10017 (August 2026) [S23, S24]. Any older source calling it a draft is out of date.
4. Cookies: RFC 6265 [S35] is current; 6265bis [S36, S37] is approved and in the RFC Editor queue; a second draft [S60] would also obsolete 6265 and adds `__Host-Http-`. NIST [S1] and OWASP [S6] already recommend `__Host-` (NIST: "SHOULD have the "__Host-" prefix and set "Path=/"").
5. Argon2 parameters: RFC 9106 [S11] (first option 2 GiB, t=1, p=4; second option 64 MiB, t=3, p=4) versus OWASP [S4] (minimum 19 MiB, t=2, p=1, with equal-defence alternatives) versus Keycloak [S55] (argon2 default iterations 5, memory not stated in the guide). Both RFC and OWASP texts are quoted above; the RFC is dated 2021, OWASP's page was read 2026-10-31.
6. Session timeouts: NIST AAL2 [S1] says overall timeout "SHOULD be no more than 24 hours" and inactivity "SHOULD be no more than 1 hour"; OWASP [S6] says "Common idle timeouts ranges are 2-5 minutes for high-value applications and 15-30 minutes for low risk applications". These are different recommendations from different bodies for different assurance purposes, not a contradiction; both are recorded.
7. Lockout thresholds: NIST [S1] gives an upper bound of 100 consecutive failures per authenticator and says organisations "MAY impose lower limits"; Keycloak [S55] ships 30 and has it off by default; OWASP [S5] gives no number.
8. pyotp: the replay helper exists on `develop` but not in release 2.10.0 [S43]. Anyone citing "pyotp supports replay prevention" must name the branch or version.
9. py_webauthn: README still links WebAuthn Level 2 [S45]; Level 3 is a Recommendation as of 2026-08-25 [S14].
10. argon2-cffi docs call RFC 9106 an "official Internet standard" [S41]; the RFC is Informational [S11].

---

## Numbered list of all distinct sources (60)

1. [S1] NIST SP 800-63B-4, Authentication and Authenticator Management (PDF), July 2025 — https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf
2. [S2] NIST CSRC page for SP 800-63B-4 (final, 07/31/25) — https://csrc.nist.gov/pubs/sp/800/63/b/4/final
3. [S3] NIST CSRC page for withdrawn SP 800-63B — https://csrc.nist.gov/pubs/sp/800/63/b/upd2/final
4. [S4] OWASP Password Storage Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
5. [S5] OWASP Authentication Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
6. [S6] OWASP Session Management Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
7. [S7] OWASP Multifactor Authentication Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html
8. [S8] OWASP Forgot Password Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html
9. [S9] OWASP Credential Stuffing Prevention Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Credential_Stuffing_Prevention_Cheat_Sheet.html
10. [S10] OWASP Passkey Security Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Passkey_Security_Cheat_Sheet.html
11. [S11] RFC 9106, Argon2 (Sept 2021) — https://www.rfc-editor.org/rfc/rfc9106.txt
12. [S12] RFC 6238, TOTP (May 2011) — https://www.rfc-editor.org/rfc/rfc6238.txt
13. [S13] RFC 4226, HOTP (Dec 2005) — https://www.rfc-editor.org/rfc/rfc4226.txt
14. [S14] W3C Web Authentication Level 3, Recommendation 25 Aug 2026 — https://www.w3.org/TR/webauthn-3/
15. [S15] FIDO Alliance, Passkeys — https://fidoalliance.org/passkeys/
16. [S16] RFC 9700, OAuth 2.0 Security BCP (Jan 2025) — https://www.rfc-editor.org/rfc/rfc9700.txt
17. [S17] RFC 7636, PKCE (Sept 2015) — https://www.rfc-editor.org/rfc/rfc7636.txt
18. [S18] RFC 8414, Authorization Server Metadata (June 2018) — https://www.rfc-editor.org/rfc/rfc8414.txt
19. [S19] RFC 7591, Dynamic Client Registration (July 2015) — https://www.rfc-editor.org/rfc/rfc7591.txt
20. [S20] RFC 7009, Token Revocation (Aug 2013) — https://www.rfc-editor.org/rfc/rfc7009.txt
21. [S21] RFC 9728, Protected Resource Metadata (Apr 2025) — https://www.rfc-editor.org/rfc/rfc9728.txt
22. [S22] RFC 8707, Resource Indicators (Feb 2020) — https://www.rfc-editor.org/rfc/rfc8707.txt
23. [S23] RFC 10017, OAuth 2.0 for Browser-Based Applications (Aug 2026) — https://www.rfc-editor.org/rfc/rfc10017.txt
24. [S24] IETF Datatracker, draft-ietf-oauth-browser-based-apps (became RFC 10017) — https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/
25. [S25] IETF Datatracker, draft-ietf-oauth-v2-1 (draft -17, updated 2026-10-30) — https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/
26. [S26] MCP specification, Versioning (current = 2026-07-28) — https://modelcontextprotocol.io/specification/versioning
27. [S27] MCP specification 2026-07-28, Authorization — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
28. [S28] MCP specification 2026-07-28, Authorization Server Discovery — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery
29. [S29] MCP specification 2026-07-28, Client Registration — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
30. [S30] MCP specification 2026-07-28, Security Considerations (authorization) — https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
31. [S31] MCP specification 2025-11-25, Authorization (previous) — https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
32. [S32] IETF Datatracker, draft-ietf-oauth-client-id-metadata-document (rev 02, 2026-07-06) — https://datatracker.ietf.org/doc/draft-ietf-oauth-client-id-metadata-document/
33. [S33] Anthropic, Authentication for connectors (Claude.ai documentation) — https://claude.com/docs/connectors/building/authentication
34. [S34] Anthropic, Claude Code documentation, MCP — https://code.claude.com/docs/en/mcp
35. [S35] RFC 6265, HTTP State Management Mechanism (Apr 2011) — https://www.rfc-editor.org/rfc/rfc6265.txt
36. [S36] draft-ietf-httpbis-rfc6265bis-22 (1 Dec 2025) — https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.txt
37. [S37] IETF Datatracker, draft-ietf-httpbis-rfc6265bis (RFC Ed Queue) — https://datatracker.ietf.org/doc/draft-ietf-httpbis-rfc6265bis/
38. [S38] RFC 8725, JWT Best Current Practices (Feb 2020) — https://www.rfc-editor.org/rfc/rfc8725.txt
39. [S39] RFC Editor JSON metadata (status checks) — https://www.rfc-editor.org/rfc/rfc6265.json (and the same pattern for the other RFC numbers)
40. [S40] PyPI, argon2-cffi 25.1.0 — https://pypi.org/project/argon2-cffi/
41. [S41] argon2-cffi docs, CHANGELOG, profiles.py — https://argon2-cffi.readthedocs.io/en/stable/parameters.html
42. [S42] PyPI, pyotp 2.10.0 — https://pypi.org/project/pyotp/
43. [S43] pyotp README, Changes.rst, totp.py, 2.10.0 wheel — https://raw.githubusercontent.com/pyauth/pyotp/develop/README.rst
44. [S44] PyPI, webauthn 3.0.1 — https://pypi.org/project/webauthn/
45. [S45] py_webauthn README, CHANGELOG, source — https://raw.githubusercontent.com/duo-labs/py_webauthn/master/CHANGELOG.md
46. [S46] PyPI, joserfc 1.7.7 — https://pypi.org/project/joserfc/
47. [S47] joserfc documentation and README — https://jose.authlib.org/en/dev/guide/registry/
48. [S48] PyPI, Authlib 1.9.1 — https://pypi.org/project/Authlib/
49. [S49] Authlib documentation and README — https://docs.authlib.org/en/latest/oauth2/index.html
50. [S50] npm, @simplewebauthn/browser 14.0.0 — https://registry.npmjs.org/@simplewebauthn/browser
51. [S51] SimpleWebAuthn README and CHANGELOG — https://raw.githubusercontent.com/MasterKale/SimpleWebAuthn/master/CHANGELOG.md
52. [S52] Have I Been Pwned API v3 documentation — https://haveibeenpwned.com/API/v3
53. [S53] Have I Been Pwned, Pwned Passwords page — https://haveibeenpwned.com/Passwords
54. [S54] HaveIBeenPwned/PwnedPasswordsDownloader README — https://raw.githubusercontent.com/HaveIBeenPwned/PwnedPasswordsDownloader/main/README.md
55. [S55] Keycloak Server Administration Guide 26.8.1 — https://www.keycloak.org/docs/latest/server_admin/index.html
56. [S56] Keycloak source, RealmManager.java @ 26.8.1 — https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/services/src/main/java/org/keycloak/services/managers/RealmManager.java
57. [S57] Keycloak source, Constants.java @ 26.8.1 — https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/server-spi-private/src/main/java/org/keycloak/models/Constants.java
58. [S58] Keycloak source, OTPPolicy.java @ 26.8.1 — https://raw.githubusercontent.com/keycloak/keycloak/26.8.1/server-spi/src/main/java/org/keycloak/models/OTPPolicy.java
59. [S59] Keycloak source, DefaultExportImportManager.java @ main — https://raw.githubusercontent.com/keycloak/keycloak/main/model/storage-private/src/main/java/org/keycloak/storage/datastore/DefaultExportImportManager.java
60. [S60] draft-ietf-httpbis-layered-cookies-02 (21 May 2026) — https://www.ietf.org/archive/id/draft-ietf-httpbis-layered-cookies-02.txt

---

## UNVERIFIED (not confirmed from a fetched source; do not cite as fact)

1. Anthropic help-center article "Building custom connectors via remote MCP servers" (https://support.claude.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers): it appears in web search results, but both curl and WebFetch got HTTP 404. I did not read it. Its contents (for example the claim that users can enter a custom client ID and secret, or that the callback URL may later move from claude.ai to claude.com) are UNVERIFIED. The equivalent statements I can cite come from [S33] only, and [S33] gives the callback as https://claude.ai/api/mcp/auth_callback with no mention of a planned domain change.
2. A separate official Claude Desktop page on MCP registration: not found. Claude Desktop is covered only through the sentence in [S33] that lists "Desktop" among the hosted surfaces sharing one client. Any Desktop-specific behaviour beyond that is UNVERIFIED.
3. Whether Claude's clients send the RFC 8707 `resource` parameter: [S27] says MCP clients MUST send it, but neither Anthropic page [S33, S34] states it in the text I searched (no match for "resource parameter" or "RFC 8707" in [S33]). Anthropic's own confirmation is UNVERIFIED.
4. Whether Claude's clients send or honour the RFC 9207 `iss` parameter: not stated in [S33]/[S34]; UNVERIFIED.
5. Repository-level maintenance facts for argon2-cffi, pyotp, py_webauthn, joserfc, Authlib and SimpleWebAuthn (archived flag, last commit date, open issues, number of maintainers): the GitHub API was not reachable from this session and I did not attach repositories. Only PyPI/npm release dates, changelog text and raw source files are verified.
6. Whether `@simplewebauthn/browser` 14.0.0 works unmodified with the JSON option format produced by py_webauthn 3.0.1: not tested; UNVERIFIED.
7. Keycloak: the Administration Guide [S55] does not state the access-token lifespan default. The value 300 seconds comes from source code constants [S57, S59], not from documentation. The `setPermanentLockout(false)`, brute-force strategy, SSL-required and login-with-email lines were read on `main` only, not on the 26.8.1 tag (the other brute-force lines were read on both).
8. Keycloak's default value for "Revoke Refresh Token" and its default argon2 memory/parallelism parameters: not stated in the fetched guide text; not looked up in source; UNVERIFIED.
9. HIBP: an official single-file download of the Pwned Passwords corpus. The fetched pages describe only the downloader tool that queries the API for every prefix [S52, S54]; I did not find, and so cannot confirm, a directly hosted file or torrent. The total corpus size and current record count were not captured.
10. HIBP rate limits for the Pwned Passwords range endpoint: the Pwned Passwords section I read states no limit and says the API is free without a key; the "Rate limiting" heading in the table of contents refers mainly to authenticated endpoints and I did not extract its text.
11. NIST 800-63B-4 section numbers for a few quotes (for example "3.1.3.3" for PSTN) come from references inside the fetched text rather than a heading I printed; the sentence text is exact.
12. The status and revision-history of RFC 10017's "BCP: 212" label: the header and Datatracker both print "BCP 212" (the number previously associated with RFC 8252, which RFC 10017's own introduction mentions as "RFC8252 or BCP 212"); I recorded it as printed and did not investigate further.
13. Whether RFC 6265bis (rfc6265bis-22) will carry exactly the text quoted here when the RFC Editor publishes it: it is an approved draft in the RFC Editor queue ("blocked: Final Approval Pending"), so wording could still change; UNVERIFIED until published.

## Controller additions (verified by the coordinator, 2026-11-02)

### [S61] ID token claims reference — Microsoft identity platform — https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference — updated_at 2026-06-15
- The payload claims table lists aud, iss, iat, idp, nbf, exp, c_hash, at_hash, aio, preferred_username, email, name, nonce, oid, roles, rh, sub, tid, sid, unique_name, uti, ver, hasgroups, groups:src1 — no `amr` claim is listed.
- "To correctly store information per-user, use `sub` or `oid` alone (which as GUIDs are unique), with `tid` used for routing or sharding if needed."
- `email`: "This value isn't guaranteed to be correct and is mutable over time. Never use it for authorization or to save data for a user."

### [S62] OpenID Connect on the Microsoft identity platform — https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc — ms.date 2026-06-30, updated_at 2026-10-06
- Authorities: `common` (personal + work/school), `organizations` (work/school only), `consumers`, or a tenant ID ("Only users from a specific Microsoft Entra tenant ... can sign in").
- Validation may include "Ensuring a certain strength of authentication has occurred, such as multifactor authentication." (no claim named for it on this page).
