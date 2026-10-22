<#import "template.ftl" as layout>
<#--
  Setting a password: reached from the console's "Change password", and forced
  when an administrator marks the credential temporary.

  `autocomplete="new-password"` on both boxes, which is the difference between
  a password manager offering to generate one and offering to autofill the OLD
  one into the field asking for the new.
-->
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('password','password-confirm'); section>
  <#if section = "header">
    Choose a new password
  <#elseif section = "form">
    <p class="itms-step-note">${msg("updatePasswordMessage")}</p>

    <form id="kc-passwd-update-form" class="itms-form" action="${url.loginAction}" method="post">
      <#-- Hidden, and present so password managers attach the new credential
           to the right account rather than saving it against nothing. -->
      <input type="text" id="username" name="username" value="${username!''}"
             autocomplete="username" readonly="readonly" style="display:none;"/>
      <input type="password" id="password" name="password" autocomplete="current-password"
             style="display:none;"/>

      <div class="itms-field">
        <label for="password-new">${msg("passwordNew")}</label>
        <input type="password" id="password-new" name="password-new" autofocus
               autocomplete="new-password"
               aria-invalid="<#if messagesPerField.existsError('password','password-confirm')>true<#else>false</#if>" />
        <#if messagesPerField.existsError('password')>
          <span id="input-error-password" aria-live="polite">
            ${kcSanitize(messagesPerField.get('password'))?no_esc}
          </span>
        </#if>
      </div>

      <div class="itms-field">
        <label for="password-confirm">${msg("passwordConfirm")}</label>
        <input type="password" id="password-confirm" name="password-confirm"
               autocomplete="new-password"
               aria-invalid="<#if messagesPerField.existsError('password-confirm')>true<#else>false</#if>" />
        <#if messagesPerField.existsError('password-confirm')>
          <span id="input-error-password-confirm" aria-live="polite">
            ${kcSanitize(messagesPerField.get('password-confirm'))?no_esc}
          </span>
        </#if>
      </div>

      <#if isAppInitiatedAction??>
        <button class="itms-primary" type="submit">${msg("doSubmit")}</button>
        <button class="itms-alt" type="submit" name="cancel-aia" value="true">${msg("doCancel")}</button>
      <#else>
        <button class="itms-primary" type="submit">${msg("doSubmit")}</button>
      </#if>
    </form>
  </#if>
</@layout.registrationLayout>
