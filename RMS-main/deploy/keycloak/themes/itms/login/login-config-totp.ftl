<#import "template.ftl" as layout>
<#--
  Enrolling an authenticator: the page "Manage MFA device" opens, and the one
  every new account meets, since CONFIGURE_TOTP ships as a default required
  action. Unstyled until now, like every page but `login.ftl`.

  THE SECRET IS OFFERED AS TEXT AS WELL AS A QR CODE, and that is not clutter:
  somebody enrolling on the machine they are reading cannot photograph their
  own screen, and a QR-only page strands them. Keycloak supplies both routes;
  base renders them as an undifferentiated list of links.
-->
<@layout.registrationLayout displayRequiredFields=false displayMessage=!messagesPerField.existsError('totp','userLabel'); section>
  <#if section = "header">
    Set up your authenticator
  <#elseif section = "form">
    <ol class="itms-steps">
      <li>${msg("loginTotpStep1")}</li>
      <li>${msg("loginTotpStep2")}</li>
      <li>${msg("loginTotpStep3")}</li>
    </ol>

    <#if mode?? && mode = "manual">
      <div class="itms-secret">
        <span class="itms-secret-label">${msg("loginTotpManualStep2")}</span>
        <code class="itms-secret-value">${totp.totpSecretEncoded}</code>
        <a class="itms-secondary-link" id="mode-barcode" href="${totp.qrUrl}">${msg("loginTotpScanBarcode")}</a>
      </div>
    <#else>
      <div class="itms-qr">
        <img id="kc-totp-secret-qr-code"
             src="data:image/png;base64, ${totp.totpSecretQrCode}"
             alt="${msg("loginTotpScanBarcode")}" />
        <a class="itms-secondary-link" href="${totp.manualUrl}"
           id="mode-manual">${msg("loginTotpUnableToScan")}</a>
      </div>
    </#if>

    <form action="${url.loginAction}" class="itms-form" id="kc-totp-settings-form" method="post">
      <div class="itms-field">
        <label for="totp">${msg("authenticatorCode")}</label>
        <input type="text" id="totp" name="totp" autocomplete="one-time-code"
               inputmode="numeric" pattern="[0-9]*" maxlength="12" autofocus
               aria-invalid="<#if messagesPerField.existsError('totp')>true<#else>false</#if>" />
        <#if messagesPerField.existsError('totp')>
          <span id="input-error-otp-code" aria-live="polite">
            ${kcSanitize(messagesPerField.get('totp'))?no_esc}
          </span>
        </#if>
      </div>

      <#-- ALWAYS SUBMITTED, NEVER GUARDED BY `mode`. Keycloak's UpdateTotp
           reads the secret back out of this field to build the credential, so
           a page that omits it posts a code with nothing to check it against
           and `OTPSecretData.getValue()` returns null -- an uncaught NPE and
           the generic "internal server error" page, not a validation message.
           This sat inside the `mode = "manual"` branch, so scanning the QR
           code -- what almost everyone does -- was the broken path and typing
           the secret by hand was the working one. -->
      <input type="hidden" id="totpSecret" name="totpSecret" value="${totp.totpSecret}" />
      <#if mode??><input type="hidden" id="mode" name="mode" value="${mode}"/></#if>

      <div class="itms-field">
        <#-- Names the device, so removing a lost phone later does not mean
             guessing which of three credentials it was. -->
        <label for="userLabel">${msg("loginTotpDeviceName")}</label>
        <input type="text" id="userLabel" name="userLabel" autocomplete="off"
               aria-invalid="<#if messagesPerField.existsError('userLabel')>true<#else>false</#if>" />
        <#if messagesPerField.existsError('userLabel')>
          <span id="input-error-otp-label" aria-live="polite">
            ${kcSanitize(messagesPerField.get('userLabel'))?no_esc}
          </span>
        </#if>
      </div>

      <button type="submit" class="itms-primary" id="saveTOTPBtn">${msg("doSubmit")}</button>
      <#if isAppInitiatedAction??>
        <button type="submit" class="itms-alt" id="cancelTOTPBtn"
                name="cancel-aia" value="true">${msg("doCancel")}</button>
      </#if>
    </form>
  </#if>
</@layout.registrationLayout>
