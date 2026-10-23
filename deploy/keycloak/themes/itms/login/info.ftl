<#import "template.ftl" as layout>
<#--
  The "that worked" page: shown after a credential action completes when the
  flow has nowhere to redirect to. Styled for the same reason the error page
  is -- a success that looks like a broken page is read as a failure, and the
  user repeats the action they just completed.
-->
<@layout.registrationLayout displayMessage=false; section>
  <#if section = "header">
    Notice
  <#elseif section = "form">
    <p class="itms-step-note">
      ${kcSanitize(message.summary)?no_esc}
      <#if requiredActions??><#list requiredActions>: <#items as reqActionItem>${kcSanitize(msg("requiredAction.${reqActionItem}"))?no_esc}<#sep>, </#items></#list></#if>
    </p>
    <#if skipLink??>
    <#elseif pageRedirectUri?has_content>
      <a class="itms-primary itms-primary-link" href="${pageRedirectUri}">${kcSanitize(msg("backTo",client.name!''))?no_esc}</a>
    <#elseif actionUri?has_content>
      <a class="itms-primary itms-primary-link" href="${actionUri}">${kcSanitize(msg("proceedWithAction"))?no_esc}</a>
    <#elseif client?? && client.baseUrl?has_content>
      <a class="itms-primary itms-primary-link" href="${client.baseUrl}">${kcSanitize(msg("backToApplication"))?no_esc}</a>
    </#if>
  </#if>
</@layout.registrationLayout>
