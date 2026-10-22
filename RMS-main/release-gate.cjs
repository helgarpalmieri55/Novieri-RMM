'use strict';
// Runs from protected main before the job with the signing environment starts.
module.exports = async ({github, context, core}) => {
  if (context.eventName !== 'workflow_dispatch' || context.ref !== 'refs/heads/main') {
    throw new Error('Agent signing requires a manual run from protected main');
  }
  const runs = await github.paginate(github.rest.actions.listWorkflowRuns, {
    ...context.repo, workflow_id: 'ci.yml', head_sha: context.sha,
    branch: 'main', event: 'push', per_page: 100,
  });
  const latest = runs.filter(r => r.head_sha === context.sha && r.head_branch === 'main' && r.event === 'push')
    .sort((a, b) => b.id - a.id)[0];
  if (!latest || latest.status !== 'completed' || latest.conclusion !== 'success') {
    throw new Error('The exact signing commit needs successful main CI (latest attempt)');
  }
  core.notice(`Signing authorized by CI run ${latest.id} for ${context.sha}`);
};
