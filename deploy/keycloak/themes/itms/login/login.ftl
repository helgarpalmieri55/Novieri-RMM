<#import "template.ftl" as layout>
<#--
  The credential form.

  THE FIELD LOOP IS THE POINT, and it is the spec's falsifier 2
  (docs/modules/console-auth/spec.md §5.2). `realm.password`, `usernameHidden`
  and `social.providers` are set by the AUTHENTICATION FLOW, not by this file.
  A template that renders its own unconditional username and password inputs
  shows a password box during a passkey-only flow -- so every block below is
  conditional on what the flow actually asked for, and this file is correct
  both before and after a passkey alternative is added to the browser flow.

  The identity-provider block renders nothing when `social.providers` is empty,
  which is why this theme can ship before the Microsoft provider exists.
-->
<#--
  The field icons: Tabler outline glyphs (24 grid, stroke 1.75, round caps
  and joins, `currentColor`), the same set the console draws from its sprite
  in `frontend/src/shell/tabler.ts`. Inlined rather than referenced because
  this theme ships to a Keycloak container with no build step and no external
  requests: a login page that fetches an icon set from a CDN is a login page
  that fails on an air-gapped install. Tabler is MIT-licensed; the notice
  ships beside the font in `resources/tabler-MIT.txt`.
-->
<#macro icon name>
  <svg class="itms-input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <#if name = "mail">
      <path d="M3 7a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v10a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-10"/><path d="M3 7l9 6l9 -6"/>
    <#elseif name = "lock">
      <path d="M5 13a2 2 0 0 1 2 -2h10a2 2 0 0 1 2 2v6a2 2 0 0 1 -2 2h-10a2 2 0 0 1 -2 -2v-6"/><path d="M11 16a1 1 0 1 0 2 0a1 1 0 0 0 -2 0"/><path d="M8 11v-4a4 4 0 1 1 8 0v4"/>
    </#if>
  </svg>
</#macro>

<@layout.registrationLayout displayMessage=!messagesPerField.existsError('username','password'); section>
  <#if section = "header">
    Sign in
  <#elseif section = "form">
    <#if realm.password>
      <form id="kc-form-login" class="itms-form" action="${url.loginAction}" method="post">
        <#if !usernameHidden??>
          <div class="itms-field">
            <label for="username">${msg("email")}</label>
            <#--
              autocomplete="username webauthn" is what offers a resident
              passkey in the browser's own account picker. It is inert until a
              passkey policy exists, and harmless then.
            -->
            <div class="itms-input">
              <@icon "mail"/>
              <input id="username" name="username" type="text" autofocus
                     autocomplete="username webauthn"
                     value="${(login.username!'')}" />
            </div>
          </div>
        </#if>

        <div class="itms-field">
          <label for="password">${msg("password")}</label>
          <div class="itms-input">
            <@icon "lock"/>
            <input id="password" name="password" type="password"
                   autocomplete="current-password" />
          </div>
        </div>

        <#-- Which stored credential the flow selected. Keycloak posts this
             back; dropping it breaks accounts with more than one. -->
        <input type="hidden" id="id-hidden-input" name="credentialId"
               <#if auth.selectedCredential?has_content>value="${auth.selectedCredential}"</#if> />

        <button class="itms-primary" name="login" id="kc-login" type="submit">
          ${msg("doLogIn")}
        </button>

        <#if realm.resetPasswordAllowed>
          <a class="itms-secondary-link" href="${url.loginResetCredentialsUrl}">
            ${msg("doForgotPassword")}
          </a>
        </#if>
      </form>
    </#if>

    <#-- `?has_content`, NOT `??`. Keycloak sets `social.providers` to an EMPTY
         LIST when the realm has no provider to offer, so `??` -- which asks
         whether the variable exists -- was true on a realm with no identity
         provider, and the page drew a divider reading "or sign in with"
         introducing nothing at all. -->
    <#if social.providers?? && social.providers?has_content>
      <div class="itms-divider"><span>${msg("identity-provider-login-label")}</span></div>
      <div class="itms-social">
        <#list social.providers as p>
          <a class="itms-social-link" href="${p.loginUrl}" id="social-${p.alias}">
            ${p.displayName!p.alias}
          </a>
        </#list>
      </div>
    </#if>
    <p class="itms-footnote">${msg("itmsAccessNotice")}</p>
  </#if>
</@layout.registrationLayout>
