<#import "template.ftl" as layout>
<#--
  THE PAGE AUDITOR A CALLED THE TERMINAL FAILURE MODE. Their words: a
  full-screen error with "no link, no button and no navigation of any kind" --
  `document.querySelectorAll('button,a')` returned an empty array -- reached by
  one click, with no way back to the product except retyping the URL.

  Two things fix that and only the second is cosmetic: a way out, and looking
  like the product it belongs to. `client.baseUrl` is present whenever the
  error happened inside a client's flow, which is every route a user reaches
  this page by; the link is conditional because it is absent for an error
  raised outside one, and a dead "back" is worse than none.
-->
<@layout.registrationLayout displayMessage=false; section>
  <#if section = "header">
    Something went wrong
  <#elseif section = "form">
    <p class="itms-step-note">${kcSanitize(message.summary)?no_esc}</p>
    <#if client?? && client.baseUrl?has_content>
      <a class="itms-primary itms-primary-link" href="${client.baseUrl}">${msg("backToApplication")}</a>
    </#if>
  </#if>
</@layout.registrationLayout>
