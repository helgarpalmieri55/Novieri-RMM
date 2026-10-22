export const onboarding = {
  page: {
    title: "Add a computer",
    description: "Install the agent on one machine, or find every machine on a network.",
    oneComputer: {
      title: "One computer",
      subtitle: "Download the installer for its platform, or copy a one-line install command.",
      getInstaller: "Get the installer",
      enrollmentTokens: "Enrollment tokens",
      note: "Either way, the computer makes its own key, which never leaves it, and gets its own certificate. It appears under Devices as soon as it checks in.",
    },
    manyNote:
      "Many computers on a network: this platform never connects to a machine, so the first machine on a new segment is always a manual install. It becomes the sweeper that finds the rest.",
    manyTitle: "Many computers on a network",
    needsAdmin: "An administrator must do this. It needs {permissions}, which your account does not have.",
    youAreHere: "You are here",
    networks: {
      title: "Your networks",
      subtitle: "Choose one to see whether its last sweep could run at all.",
      empty: "No network is registered yet. Step 1 above.",
      network: "Network",
      lastSwept: "Last swept",
      unnamed: "unnamed",
      selected: "Selected",
      checkThisOne: "Check this one",
      selectedAria: "{prefix} selected",
      checkAria: "Check {prefix}",
    },
  },
  state: {
    done: "Done",
    partial: "Partly done",
    blocked: "Blocked",
    unknown: "Checking…",
  },
  journey: {
    register: {
      title: "Register the network",
      toLabel: "Open IP Address Management",
      loading: "Loading your registered networks…",
      none: "No network is registered.",
      noneRemedy:
        "A sweep targets a registered subnet, so this is where everything starts. Register the prefix you want to find machines on.",
      done: { one: "{count} network registered.", other: "{count} networks registered." },
    },
    authorize: {
      title: "Authorize scanning here",
      toLabel: "Open scan authorizations",
      loading: "Loading scan authorizations…",
      nothingYet: "Nothing to authorize yet: no network is registered.",
      finishStep1: "Finish step 1 first.",
      remedy:
        "Scanning is deny-by-default: registering a subnet does not authorize probing it, and a sweep with no covering grant is refused before any job is created. A human with this permission puts their name on every packet this platform emits.",
      none: "No registered network is authorized for scanning.",
      partial: "{covered} of {total} networks are authorized for scanning.",
      done: { one: "All {count} registered network is authorized.", other: "All {count} registered networks are authorized." },
    },
    sweep: {
      title: "Sweep for what is out there",
      loading: "Loading sweep history…",
      noneAuthorized: "No authorized network to sweep.",
      noneAuthorizedRemedy: "Finish step 2 first. A sweep outside an authorization is refused, not attempted.",
      thisNetwork: "this network",
      noAgent: "The last sweep of {network} could not run: no enrolled agent on that segment.",
      noAgentRemedy:
        "A sweep is performed BY an agent inside the segment. This platform never probes a network itself. Install the agent by hand on one machine there (step 5 has the command), and it becomes the sweeper for the rest.",
      noAgentToLabel: "Get an install command",
      chooseNetwork: "Choose a network",
      open: "Open {prefix}",
      noneSwept: "No authorized network has been swept.",
      noneSweptRemedy: "Run a sweep from the subnet page. It sends ICMP/ARP from an agent already inside the segment.",
      partial: "{swept} of {total} authorized networks have been swept.",
      partialRemedy: "Sweep the rest, or leave them: a sweep is documentation currency, not monitoring.",
      done: { one: "All {count} authorized network has been swept.", other: "All {count} authorized networks have been swept." },
    },
    review: {
      title: "Review what answered",
      toLabel: "Open Discovered",
      loading: "Loading what the last sweeps found…",
      blind: {
        one: "{count} network could not be swept at all. Nothing looked there, which is not the same as nothing being there.",
        other: "{count} networks could not be swept at all. Nothing looked there, which is not the same as nothing being there.",
      },
      alsoInFleet:
        "A sweep may also write what it found straight into the fleet list; those rows are marked as recorded by a sweep, and nobody has confirmed they are machines. Look at both.",
      nothingWaiting: "Nothing is waiting for review on Discovered.",
      goBackStep3: "Go back to step 3 for the networks nothing could look at.",
      answered: {
        one: "{count} address answered that the inventory cannot account for.",
        other: "{count} addresses answered that the inventory cannot account for.",
      },
      answeredRemedy:
        "Check them before you put an agent on anything. A ping reply is not a machine: a sweep returns printers, phones, a colleague's laptop on the wrong VLAN and the coffee machine, and Discovered labels how strong the evidence is for each one.",
    },
    agent: {
      title: "Get the agent onto them",
      toLabel: "Open Agent packages",
      loading: "Loading the package catalogue…",
      none: "No published release has an uploaded artefact.",
      noneRemedy:
        "This platform never builds or fetches the agent. The catalogue holds bytes somebody published. Nothing can be installed until one is there.",
      ready: { one: "{count} release ready to install.", other: "{count} releases ready to install." },
      readyRemedy:
        "One machine at a time: an install command carrying a single-use token. For a whole segment at once, use Managed deployment: GPO or Intune, with no credential in the package.",
    },
  },
} as const;
