const test = require('node:test');
const assert = require('node:assert/strict');
const gate = require('../../release-gate.cjs');
const good = {id: 1, head_sha: 'abc', head_branch: 'main', event: 'push', status: 'completed', conclusion: 'success'};
function run(runs=[good], overrides={}) {
  return gate({github:{rest:{actions:{listWorkflowRuns:()=>{}}},paginate:async()=>runs},
    context:{eventName:'workflow_dispatch',ref:'refs/heads/main',sha:'abc',repo:{owner:'o',repo:'r'},...overrides},core:{notice:()=>{}}});
}
test('allows the exact tested main commit',()=>run());
for (const [name,runs,ctx] of [
 ['branch', [good], {ref:'refs/heads/untrusted'}],
 ['tag', [good], {ref:'refs/tags/agent-v1.2.3'}],
 ['push', [good], {eventName:'push'}],
 ['missing CI', [], {}],
 ['another SHA', [{...good,head_sha:'def'}], {}],
 ['PR CI', [{...good,event:'pull_request'}], {}],
 ['other branch', [{...good,head_branch:'untrusted'}], {}],
 ['failed CI', [{...good,conclusion:'failure'}], {}],
 ['pending rerun', [good,{...good,id:2,status:'in_progress',conclusion:null}], {}],
 ['new failed run', [good,{...good,id:2,conclusion:'failure'}], {}],
]) test('rejects '+name,()=>assert.rejects(run(runs,ctx)));
