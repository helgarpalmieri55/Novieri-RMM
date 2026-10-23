<#import "template.ftl" as layout>
<#--
  Choosing how to sign in -- and on this deployment, the page a passkey is
  actually picked on. `keycloak-init` binds a browser flow with WebAuthn
  passwordless as an ALTERNATIVE beside the password form, and "try another
  way" lands here.

  It was rendering through `parent=base`: bare HTML, no styling, in the middle
  of the one flow every user passes through. The operator signed in with a
  passkey through this page the day it was written.

  Each option is a full-width BUTTON, not a link inside a list item: it is an
  action with a large target, on a page whose whole purpose is picking one.
-->
<@layout.registrationLayout displayInfo=false; section>
  <#if section = "header">
    Choose how to sign in
  <#elseif section = "form">
    <form id="kc-select-credential-form" class="itms-form"
          action="${url.loginAction}" method="post">
      <ul class="itms-authenticators">
        <#list auth.authenticationSelections as selection>
          <li>
            <button class="itms-authenticator" type="submit"
                    name="authenticationExecution" value="${selection.authExecId}">
              <span class="itms-authenticator-name">${msg('${selection.displayName}')}</span>
              <span class="itms-authenticator-help">${msg('${selection.helpText}')}</span>
              <#-- Two lines, and they must STAY two lines: the operator's
                   screenshot showed "Username and password Sign in by entering
                   your username and password." as one run-on sentence, because
                   the spans were inline in an unstyled list. The grid in
                   `.itms-authenticator` is what separates them. -->
            </button>
          </li>
        </#list>
      </ul>
    </form>

    <p class="itms-footnote">${msg("itmsAccessNotice")}</p>
  </#if>
</@layout.registrationLayout>
