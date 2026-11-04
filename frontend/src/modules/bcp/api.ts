// TanStack Query hooks for the BCP/DR module, built on the shared ApiClient
// (useApi()). There is no live BCP/DR event stream — every mutation below
// invalidates the relevant query keys on success so the UI refetches, rather
// than subscribing to a websocket event that doesn't exist for this module
// (mirrors modules/change/api.ts's rationale).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../../lib/ApiContext";
import type { Me } from "../../lib/api";
import { requireKnown } from "../../lib/clientSelection";
import type {
  FindingCreateRequest,
  FindingUpdateRequest,
  PlanCreateRequest,
  PlanUpdateRequest,
  StepUpsertRequest,
  TestCreateRequest,
  TestRecordRequest,
} from "./types";

// --------------------------------------------------------------------------
// The reader's timezone (UX remediation cycle 5)
// --------------------------------------------------------------------------

/**
 * The zone the signed-in operator works in, for `lib/datetime`.
 *
 * A HOOK AND NOT A PROP, the same reasoning as `modules/health/api.ts` and
 * `modules/sla/api.ts`. Every instant this module renders sits inside a table
 * row, a dialog, or a `<Card>` well below the page component, and one test's
 * `scheduled_for` is rendered by `PlanDetail`, by `TestRecorder` and by
 * `BcpReport` -- none of which owns the others. Threading `timezone` down
 * those chains means editing files with nothing to do with this module. This
 * reads the SAME `["me"]` query the pages already keep warm
 * (`staleTime: 60_000`), so it costs no extra request.
 *
 * WHOSE ZONE THIS IS: the READER's, and that is right for every value here.
 * `created_at`, `last_reviewed_at`, `scheduled_for`, `executed_at`,
 * `captured_at` and the frozen snapshot's backup timestamps are all INSTANTS
 * -- single moments on a server clock, not wall-clock times belonging to some
 * other zone the way an SLA calendar's working windows are.
 *
 * `null` when the profile carries no preference; every helper in
 * `lib/datetime` falls back to the browser zone on `null`, which is the old
 * behaviour and is right for someone who never set one.
 */
export function useUserTimezone(): string | null {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  return me.data?.timezone ?? null;
}

export function usePlans() {
  const api = useApi();
  return useQuery({ queryKey: ["bcp-plans"], queryFn: () => api.listPlans() });
}

export function useCreatePlanMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    // The body is BUILT HERE, inside the mutation, not before it: a body that
    // cannot name its client yet throws `ClientNotChosen`
    // (`lib/clientSelection.ts`), which then fails the mutation -- so the
    // form's error surface says why and nothing is sent.
    mutationFn: (build: () => PlanCreateRequest) => api.createPlan(build()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["bcp-plans"] });
    },
  });
}

export function usePlan(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["bcp-plan", id],
    queryFn: () => api.getPlan(id as string),
    enabled: id != null,
  });
}

export function useUpdatePlanMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: PlanUpdateRequest }) =>
      api.updatePlan(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["bcp-plans"] });
      void queryClient.invalidateQueries({ queryKey: ["bcp-plan", variables.id] });
    },
  });
}

export function useReviewPlanMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.reviewPlan(id),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: ["bcp-plans"] });
      void queryClient.invalidateQueries({ queryKey: ["bcp-plan", id] });
      void queryClient.invalidateQueries({ queryKey: ["bcp-plan-status"] });
    },
  });
}

export function usePlanStatus() {
  const api = useApi();
  return useQuery({ queryKey: ["bcp-plan-status"], queryFn: () => api.planStatus() });
}

// --------------------------------------------------------------------------
// The lists the plan editor points at (UX remediation cycle 6)
// --------------------------------------------------------------------------

/**
 * The endpoints a plan's scope can name.
 *
 * FETCHED ONLY WHILE THE EDITOR IS OPEN (`enabled`): the plan list and the plan
 * detail page have no use for it, and a device list on every visit to /bcp is a
 * request spent on a control nobody has opened.
 *
 * `retry: false` because a 403 -- a continuity owner holding `bcp.manage` but
 * not `devices.read` -- is an answer, not a flake, and retrying it only delays
 * the empty hint that explains it.
 *
 * ONE CLIENT'S, ASKED OF THE SERVER (phase 3 review, F4). A plan names only
 * its client's devices (design §3.3), and `GET /devices` answers one page --
 * 100 by default. Filtered in the browser, the page read with every client
 * chosen could hold none of the plan's client's machines, and the editor
 * said "Add a computer" to a client that has them. So the read carries the
 * plan's client (`client_id`), the key names it, and nothing is asked until
 * it is known.
 */
export function useScopeDevices(enabled: boolean, clientId: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: ["bcp-scope-devices", clientId],
    queryFn: async () => (await api.listDevices({ clientId: requireKnown(clientId) })).items,
    enabled: enabled && clientId !== null,
    retry: false,
    staleTime: 60_000,
  });
}

/**
 * The documentation articles a plan's `procedure_refs` must name.
 *
 * EVERY entry in `procedure_refs` is resolved against `doc_articles.id` by
 * `bcp/service.py#_validate_procedure_refs`, so this is not a convenience: it
 * is the only way to produce a value the server will accept.
 */
/**
 * The device groups a plan's scope can name (cycle 9).
 *
 * FETCHED ONLY WHILE THE EDITOR IS OPEN, and `retry: false`, for the same
 * reason `useScopeDevices` above gives: this feeds a picker rather than the
 * plan itself, and a caller who may manage plans but not list groups should
 * meet the picker's `emptyHint` rather than three silent retries.
 *
 * ONE CLIENT'S, ASKED OF THE SERVER, like the devices (phase 3 final review C,
 * Important 5; operator decision 1). These two used to read one page (100) of
 * every client's in the request's scope and filter it here, so with every
 * client chosen a plan's client could lose groups or articles it has.
 * `GET /device-groups` and `GET /docs/articles` take `client_id` now (review
 * A6), so the read carries the plan's client, the key names it, and nothing
 * is asked until it is known.
 */
export function useScopeGroups(enabled: boolean, clientId: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: ["bcp-scope-groups", clientId],
    queryFn: async () => (await api.listDeviceGroups({ client_id: requireKnown(clientId) })).items,
    enabled: enabled && clientId !== null,
    retry: false,
    staleTime: 60_000,
  });
}

/** The plan's client's articles and every client's: what its procedures may name. */
export function useProcedureArticles(enabled: boolean, clientId: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: ["bcp-procedure-articles", clientId],
    queryFn: async () => (await api.listArticles({ client_id: requireKnown(clientId) })).items,
    enabled: enabled && clientId !== null,
    retry: false,
    staleTime: 60_000,
  });
}

export function usePlanTests(planId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["bcp-plan-tests", planId],
    queryFn: () => api.listPlanTests(planId as string),
    enabled: planId != null,
  });
}

export function useCreateTestMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ planId, body }: { planId: string; body: TestCreateRequest }) =>
      api.createTest(planId, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["bcp-plan-tests", variables.planId] });
      void queryClient.invalidateQueries({ queryKey: ["bcp-plan-status"] });
    },
  });
}

export function useTest(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["bcp-test", id],
    queryFn: () => api.getTest(id as string),
    enabled: id != null,
  });
}

export function useRecordTestMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: TestRecordRequest }) =>
      api.recordTest(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["bcp-test", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["bcp-test-report", variables.id] });
      void queryClient.invalidateQueries({ queryKey: ["bcp-plan-status"] });
    },
  });
}

export function useUpsertStepMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ testId, body }: { testId: string; body: StepUpsertRequest }) =>
      api.upsertStep(testId, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["bcp-test-report", variables.testId] });
    },
  });
}

export function useFindings(testId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["bcp-test-findings", testId],
    queryFn: () => api.listFindings(testId as string),
    enabled: testId != null,
  });
}

export function useCreateFindingMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ testId, body }: { testId: string; body: FindingCreateRequest }) =>
      api.createFinding(testId, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["bcp-test-findings", variables.testId] });
      void queryClient.invalidateQueries({ queryKey: ["bcp-test-report", variables.testId] });
    },
  });
}

export function useUpdateFindingMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; testId: string; body: FindingUpdateRequest }) =>
      api.updateFinding(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["bcp-test-findings", variables.testId] });
      void queryClient.invalidateQueries({ queryKey: ["bcp-test-report", variables.testId] });
    },
  });
}

export function useTestReport(testId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["bcp-test-report", testId],
    queryFn: () => api.testReport(testId as string),
    enabled: testId != null,
  });
}

export function usePeriodReport(from: string | undefined, to: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["bcp-period-report", from, to],
    queryFn: () => api.periodReport(from as string, to as string),
    enabled: from != null && from !== "" && to != null && to !== "",
  });
}
