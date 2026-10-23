<#import "template.ftl" as layout>
<#--
  The page you meet after leaving a sign-in half-finished -- a coffee, a
  meeting, a tab restored the next morning. Base renders two bare links.

  Both routes are kept and named for what they DO rather than for the state
  machine they belong to: "restart" throws the attempt away and begins again,
  "continue" resumes it where it stopped. A user who cannot tell those apart
  picks the one that loses their place.
-->
<@layout.registrationLayout displayMessage=false; section>
  <#if section = "header">
    This page expired
  <#elseif section = "form">
    <p class="itms-step-note">${msg("pageExpiredMsg1")}</p>
    <a id="loginRestartLink" class="itms-primary itms-primary-link"
       href="${url.loginRestartFlowUrl}">${msg("doClickHere")}</a>
    <a id="loginContinueLink" class="itms-secondary-link"
       href="${url.loginAction}">${msg("pageExpiredMsg2")} ${msg("doClickHere")}</a>
  </#if>
</@layout.registrationLayout>
