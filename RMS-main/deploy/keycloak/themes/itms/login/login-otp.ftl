<#import "template.ftl" as layout>
<#--
  The second step of a password sign-in: the authenticator code.

  WHY THIS FILE EXISTS. `parent=base` in theme.properties supplies templates
  and NO styling, so every page this theme does not override renders as bare
  HTML. Until now the theme shipped `login.ftl` alone -- so the first screen
  was branded and the second, which every single sign-in passes through
  (CONFIGURE_TOTP is a default required action), was an unstyled form on a grey
  page. The operator sent a screenshot of it: serif "it-suite", a naked input,
  a default Sign In button. First impression of the product, on the way in.

  THE SHAPE IS THE OPERATOR'S OWN REFERENCE, and worth stating because it is
  not the shape Keycloak suggests: their mock renders the SECOND step as a
  continuation of the first -- "Password accepted", the account being signed
  in, then the active field -- rather than as a fresh page that has forgotten
  what just happened. Two steps is a Keycloak constraint (the password form and
  the OTP form are separate authenticator executions and no stock authenticator
  takes both), but "feels like two pages" is not; that part was ours.

  WHAT IS DELIBERATELY NOT COPIED FROM THE MOCK: it shows the email and a
  filled password box above the code field. Rendering a password box full of
  dots that maps to nothing is theatre -- it implies the page holds a
  credential it does not have. The account is shown instead (base's template
  already renders it, with the restart link), which is the same reassurance
  and is true.
-->
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('totp'); section>
  <#if section = "header">
    Two-step verification
  <#elseif section = "form">
    <#-- Names what just succeeded and what is being asked for. The base page
         asked for "One-time code" with no indication that a password had been
         accepted, so a wrong password and a missing authenticator looked the
         same from here. -->
    <p class="itms-step-note">
      <#-- v3's ok pill, icon first: the state is carried by the word and the
           glyph, never by the colour alone. -->
      <span class="itms-pill itms-pill-ok">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0"/><path d="M9 12l2 2l4 -4"/>
        </svg>
        ${msg("itmsPasswordAccepted")}
      </span>
      <br>
      ${msg("loginTotpStepNote")}
    </p>

    <form id="kc-otp-login-form" class="itms-form" action="${url.loginAction}" method="post">
      <#-- More than one enrolled authenticator: Keycloak needs to know which
           one the code came from. Rendered only when there IS a choice. -->
      <#if otpLogin?? && otpLogin.userOtpCredentials?size gt 1>
        <div class="itms-field">
          <label for="selectedCredentialId">${msg("loginChooseAuthenticator")}</label>
          <select id="selectedCredentialId" name="selectedCredentialId">
            <#list otpLogin.userOtpCredentials as credential>
              <option value="${credential.id}"
                      <#if credential.id == otpLogin.selectedCredentialId>selected</#if>>
                ${credential.userLabel}
              </option>
            </#list>
          </select>
        </div>
      </#if>

      <div class="itms-field">
        <label for="otp">${msg("loginOtpOneTime")}</label>
        <#--
          inputmode="numeric" and autocomplete="one-time-code" are what make
          this bearable on a phone: the numeric keypad opens, and iOS and
          Android offer the code from the notification. `autofocus` because
          this page has exactly one thing to do.
        -->
        <div class="itms-input">
          <svg class="itms-input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M11.46 20.846a12 12 0 0 1 -7.96 -14.846a12 12 0 0 0 8.5 -3a12 12 0 0 0 8.5 3a12 12 0 0 1 -.09 7.06"/>
            <path d="M15 19l2 2l4 -4"/>
          </svg>
          <input id="otp" name="otp" type="text" autofocus
               autocomplete="one-time-code" inputmode="numeric"
               pattern="[0-9]*" maxlength="12"
                 aria-invalid="<#if messagesPerField.existsError('totp')>true<#else>false</#if>" />
        </div>
        <#-- What to type, before getting it wrong. -->
        <p class="itms-help">${msg("loginOtpHelp")}</p>
        <#if messagesPerField.existsError('totp')>
          <span id="input-error-otp-code" aria-live="polite">
            ${kcSanitize(messagesPerField.get('totp'))?no_esc}
          </span>
        </#if>
      </div>

      <button class="itms-primary" name="login" id="kc-login" type="submit">
        ${msg("doLogIn")}
      </button>
    </form>

    <p class="itms-footnote">${msg("itmsAccessNotice")}</p>
  </#if>
</@layout.registrationLayout>
