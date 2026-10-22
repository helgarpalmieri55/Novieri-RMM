<#import "template.ftl" as layout>
<#--
  Where a passkey step lands when the browser reports a failure: the person
  cancelled, the device refused, or -- the case that prompted this page -- the
  operating system could not reach the password manager it wanted to save into.
  webauthnRegister.js and webauthnAuthenticate.js both post the browser's own
  error string back to the server, which renders this.

  SHARED BY CREATING AND BY SIGNING IN, so nothing below may say "create". A
  heading that assumed registration would be false on a failed sign-in.

  displayMessage=true is kept from base: it shows the server's message, which
  carries the browser's own error text (NotAllowedError, AbortError...). That is
  the one line that tells an operator which side failed, so it is not hidden.

  THE CONTRACT IS KEYCLOAK 26.2's (base/login/webauthn-error.ftl, keycloak-themes
  26.2.4): `refreshPage` fills `isSetRetry` and `authenticationExecution` and
  posts `kc-error-credential-form`, which is what makes "Try again" restart the
  step rather than reload a dead page. Pinned by
  `backend/tests/test_console_auth_realm.py`.
-->
<@layout.registrationLayout displayMessage=true; section>
  <#if section = "header">
    That passkey step didn't finish
  <#elseif section = "form">
    <p class="itms-step-note">${msg("itmsPasskeyErrorIntro")}</p>

    <form id="kc-error-credential-form" class="itms-form" action="${url.loginAction}" method="post">
      <input type="hidden" id="executionValue" name="authenticationExecution"/>
      <input type="hidden" id="isSetRetry" name="isSetRetry"/>
    </form>

    <button type="button" class="itms-primary" id="kc-try-again" name="try-again"
            onclick="refreshPage()">${kcSanitize(msg("doTryAgain"))?no_esc}</button>

    <#if isAppInitiatedAction??>
      <form action="${url.loginAction}" class="itms-form" id="kc-webauthn-settings-form" method="post">
        <button type="submit" class="itms-alt" id="cancelWebAuthnAIA"
                name="cancel-aia" value="true">${msg("doCancel")}</button>
      </form>
    </#if>

    <#-- After the action, not before it: the page exists to be retried, and
         advice stacked above the only button pushed it against the note. -->
    <div class="itms-note" role="note">
      <span class="itms-note-title">${msg("itmsPasskeyTroubleTitle")}</span>
      <span>${msg("itmsPasskeyTroubleBody")}</span>
    </div>

    <script type="text/javascript">
      refreshPage = () => {
        document.getElementById('isSetRetry').value = 'retry';
        document.getElementById('executionValue').value = '${execution}';
        document.getElementById('kc-error-credential-form').requestSubmit();
      }
    </script>
  </#if>
</@layout.registrationLayout>
