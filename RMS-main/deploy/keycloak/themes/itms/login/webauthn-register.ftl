<#import "template.ftl" as layout>
<#import "password-commons.ftl" as passwordCommons>
<#--
  Creating a passkey: the page every new account meets, since enrolment ends in
  the `webauthn-register-passwordless` required action. Keycloak renders both
  that and the plain `webauthn-register` action through this one template.

  UNTIL THIS FILE EXISTED the page fell through to `base`, which supplies
  markup and no styling: a lone "Sign out from other devices" checkbox and a
  Register button, with nothing saying what the button does or what to do when
  the browser's own dialog fails. It did fail -- Windows offered to save the
  passkey to Microsoft Password Manager, could not reach it, and left the
  person on a bare page with no next step. That failure is the operating
  system's, not this server's, but a page that does not say so reads as broken.

  THE CONTRACT BELOW IS KEYCLOAK 26.2's, COPIED, NOT RECALLED. The form, the six
  hidden fields and the input keys passed to `registerByWebAuthn` are exactly
  those of base/login/webauthn-register.ftl in keycloak-themes 26.2.4, because
  base/login/resources/js/webauthnRegister.js fills those six fields by id and
  posts the form. A field this page drops is a field the server never receives.
  That is not hypothetical: login-config-totp.ftl once scoped its hidden
  `totpSecret` to one branch, and every QR enrolment ended in a server NPE.
  `backend/tests/test_console_auth_realm.py` pins every one of them.

  THE BUTTON IS OUTSIDE THE FORM AND IS type="button". The JS submits the form
  itself, after `navigator.credentials.create()` resolves or rejects. A submit
  button inside the form would post it empty the instant it was clicked, before
  the browser had created anything.
-->
<@layout.registrationLayout; section>
  <#if section = "header">
    Set up your passkey
  <#elseif section = "form">
    <p class="itms-step-note">${msg("itmsPasskeyIntro")}</p>

    <ol class="itms-steps">
      <li>${msg("itmsPasskeyStep1")}</li>
      <li>${msg("itmsPasskeyStep2")}</li>
    </ol>

    <form id="register" class="itms-form" action="${url.loginAction}" method="post">
      <input type="hidden" id="clientDataJSON" name="clientDataJSON"/>
      <input type="hidden" id="attestationObject" name="attestationObject"/>
      <input type="hidden" id="publicKeyCredentialId" name="publicKeyCredentialId"/>
      <input type="hidden" id="authenticatorLabel" name="authenticatorLabel"/>
      <input type="hidden" id="transports" name="transports"/>
      <input type="hidden" id="error" name="error"/>
      <@passwordCommons.logoutOtherSessions/>
    </form>

    <button type="button" class="itms-primary" id="registerWebAuthn">${msg("itmsPasskeyCreate")}</button>

    <#if !isSetRetry?has_content && isAppInitiatedAction?has_content>
      <form action="${url.loginAction}" class="itms-form" id="kc-webauthn-settings-form" method="post">
        <button type="submit" class="itms-alt" id="cancelWebAuthnAIA"
                name="cancel-aia" value="true">${msg("doCancel")}</button>
      </form>
    </#if>

    <#-- The one failure people actually hit, answered where they will be
         looking when it happens rather than on a help page they will not
         find. -->
    <div class="itms-note" role="note">
      <span class="itms-note-title">${msg("itmsPasskeyTroubleTitle")}</span>
      <span>${msg("itmsPasskeyTroubleBody")}</span>
    </div>

    <script type="module">
      import { registerByWebAuthn } from "${url.resourcesPath}/js/webauthnRegister.js";
      const registerButton = document.getElementById('registerWebAuthn');
      registerButton.addEventListener("click", function() {
        const input = {
          challenge : '${challenge}',
          userid : '${userid}',
          username : '${username}',
          signatureAlgorithms : [<#list signatureAlgorithms as sigAlg>${sigAlg?c},</#list>],
          rpEntityName : '${rpEntityName}',
          rpId : '${rpId}',
          attestationConveyancePreference : '${attestationConveyancePreference}',
          authenticatorAttachment : '${authenticatorAttachment}',
          requireResidentKey : '${requireResidentKey}',
          userVerificationRequirement : '${userVerificationRequirement}',
          createTimeout : ${createTimeout},
          excludeCredentialIds : '${excludeCredentialIds}',
          initLabel : "${msg("webauthn-registration-init-label")?no_esc}",
          initLabelPrompt : "${msg("webauthn-registration-init-label-prompt")?no_esc}",
          errmsg : "${msg("webauthn-unsupported-browser-text")?no_esc}"
        };
        registerByWebAuthn(input);
      });
    </script>
  </#if>
</@layout.registrationLayout>
