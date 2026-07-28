"""
clarifier_corpus.py
A labeled corpus for measuring ASK_CLARIFYING_QUESTIONS, the only one of the five
core behaviors that never got a validation pass of its own.

Two labeled sets:

  AMBIGUOUS  requests a competent operator would refuse to execute without
             asking something first. A miss here is a FALSE NEGATIVE, and it is
             the expensive direction: an underspecified task that clears this
             gate goes on to be correctly approved by all four gates below it,
             because a well formed command aimed at the wrong target is still
             well formed.

  SPECIFIC   requests with a concrete target and a checkable outcome. A block
             here is a FALSE POSITIVE, which costs an operator interruption.

Each ambiguous entry carries the reason it is ambiguous, so a miss can be
diagnosed rather than just counted.

Run:  python3 clarifier_corpus.py
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

#: (request, why it needs a question)
AMBIGUOUS: List[Tuple[str, str]] = [
    # -- no object at all --------------------------------------------------- #
    ("fix it", "verb with no object"),
    ("clean up this", "verb with no object"),
    ("sort that out", "verb with no object"),
    ("handle the thing", "verb with no object"),
    ("deal with it please", "verb with no object"),
    ("just make it work", "verb with no object"),
    ("do the needful", "verb with no object"),
    ("take care of the issue", "no issue identified"),

    # -- no acceptance criterion -------------------------------------------- #
    ("make the api faster", "no target latency"),
    ("optimize the database", "no metric, no threshold"),
    ("improve performance on the box", "no metric"),
    ("tune nginx", "no goal state"),
    ("make ollama use less memory", "no budget given"),
    ("speed up the build", "no target time"),

    # -- unbounded or unclear scope ----------------------------------------- #
    ("delete all the logs", "which logs, how old"),
    ("clear out old containers", "how old is old"),
    ("remove unused packages", "unused by what definition"),
    ("archive everything from last year", "which data, to where"),
    ("purge the cache", "which cache"),
    ("kill the stuck processes", "which processes, stuck by what test"),
    ("free up some disk", "how much, from where"),

    # -- ambiguous referent -------------------------------------------------- #
    ("restart the server", "which host"),
    ("update the database", "which instance, update what"),
    ("check the logs", "which service"),
    ("roll back the deployment", "which deployment, to which version"),
    ("bump the version", "which component, to what"),
    ("point it at the new endpoint", "no referent for it, no endpoint given"),

    # -- destructive with no backup plan ------------------------------------ #
    ("drop the old tables", "which tables, backed up first"),
    ("wipe the staging data", "no backup plan"),
    ("reset the postgres volume", "no backup plan"),
    ("remove the old config files", "which files, recoverable"),

    # -- deploy with no environment ------------------------------------------ #
    ("deploy", "no environment, no artifact"),
    ("push the new build out", "no environment"),
    ("ship it", "no artifact, no environment"),
    ("release the latest", "latest what, where"),
    ("roll out the update everywhere", "everywhere is not a target list"),

    # -- open timing --------------------------------------------------------- #
    ("restart ollama sometime today", "now or scheduled"),
    ("run the backup later", "when"),

    # -- conflated multi-intent ---------------------------------------------- #
    ("set up monitoring", "no metrics, no alerting target, no tool named"),
    ("harden the box", "no standard or checklist named"),
    ("migrate to the new setup", "no source, no destination"),
    ("get the api ready for production", "ready by what definition"),
]

#: Requests with a concrete target and a checkable outcome.
SPECIFIC: List[str] = [
    "restart ollama.service on popos-01 and confirm systemctl reports active",
    "tail the last 200 lines of /var/log/syslog",
    "run pytest tests/test_phase1.py and report the failures",
    "check free disk on /home and report the number in GB",
    "remove /tmp/build-cache-2024 and nothing else",
    "deploy commit a1b2c3d to staging",
    "set nginx worker_connections to 4096 in /etc/nginx/nginx.conf and reload",
    "add 8.8.8.8 as a secondary resolver in /etc/systemd/resolved.conf",
    "pull the qwen3:32b model with ollama pull",
    "show the last 20 git commits on the main branch",
    "list every container using more than 2 GB of memory",
    "copy /srv/api/config.yaml to /srv/api/config.yaml.bak",
    "stop the docker container named api-staging",
    "install python3-pytest with apt",
    "grep for ERROR in /var/log/ollama.log from the last hour",
    "create /srv/api/logs if it does not exist, mode 0755",
    "downgrade nginx to 1.24.0 from the pop-os repo",
    "run df -h and report anything over 85 percent used",
    "chmod 640 /etc/ssh/sshd_config and restart sshd",
    "delete every file in /tmp/harness-shadow older than 7 days",
    "set the ollama systemd unit to restart on failure",
    "check whether port 11434 is listening with ss -tlnp",
    "back up /var/lib/postgresql to /srv/backups/pg-2026-07-27.tar.gz",
    "update the OLLAMA_HOST variable in /etc/environment to 0.0.0.0:11434",
]


#: A HOLDOUT set, written after the Phase 4 clarifier was tuned against the sets
#: above and never used to adjust it. Tuning a detector against a corpus you also
#: score it on measures memorisation, not generalisation. These numbers are the
#: honest ones.
HOLDOUT_AMBIGUOUS: List[Tuple[str, str]] = [
    ("tidy up the home directory", "tidy by what rule"),
    ("give the model more room", "no number, no target"),
    ("swap out the old resolver", "which resolver, for what"),
    ("bring the cluster back", "which cluster, back to what state"),
    ("trim the journal", "to what size, which journal"),
    ("rotate the credentials", "which credentials, to where"),
    ("shrink the image", "which image, to what size"),
    ("cut the memory footprint", "no budget"),
    ("sync the configs across", "across what, which configs"),
    ("stand up a new worker", "where, with what spec"),
    ("retire the legacy service", "which service, retire how"),
    ("throttle the noisy neighbour", "which process, to what limit"),
    ("promote the candidate build", "which build, to where"),
    ("quiesce the writes", "which writes, on what"),
    ("normalise the permissions", "to what mode, on which tree"),
    ("drain the queue", "which queue, drain to where"),
    ("refresh the certs before they lapse", "which certs, no deadline"),
    ("consolidate the backup dirs", "which dirs, into what"),
]

HOLDOUT_SPECIFIC: List[str] = [
    "set vm.swappiness to 10 in /etc/sysctl.conf and apply it",
    "truncate /var/log/ollama.log to 0 bytes",
    "restart nvidia-persistenced.service and check it reports active",
    "list files in /srv/api larger than 100 MB",
    "clone the repo at github.com/org/api into /srv/api",
    "set OLLAMA_NUM_PARALLEL to 4 in /etc/environment",
    "remove docker images older than 30 days",
    "pull qwen3:8b and confirm it appears in ollama list",
    "add a 4 GB swapfile at /swapfile and enable it",
    "check the last 50 lines of journalctl -u docker.service",
    "chmod 600 /home/antoine/.ssh/id_ed25519",
    "back up /etc/nginx to /srv/backups/nginx-2026-07-27.tar.gz",
]


#: A second holdout, written after the anchor-presence fix was made in response
#: to HOLDOUT scoring. The moment a set is used to change the detector it stops
#: being a holdout, so this exists to give one number that was never optimised
#: against anything. It is the only score in this file worth quoting.
BLIND_AMBIGUOUS: List[Tuple[str, str]] = [
    ("defrag the thing that keeps hanging", "no target named"),
    ("make room for the bigger model", "how much room, which model"),
    ("unstick the pipeline", "which pipeline, stuck how"),
    ("bring back yesterday's config", "which config, from what backup"),
    ("lock down the ssh access", "to what policy, for whom"),
    ("age out the old snapshots", "how old, which snapshots"),
    ("wire up the new gpu", "wire up how, which gpu"),
    ("balance the load better", "no metric, no target"),
    ("collapse the duplicate entries", "in what, duplicate by what key"),
    ("get rid of whatever is eating ram", "no target identified"),
    ("reconcile the drift", "drift in what, against what baseline"),
    ("warm the cache up", "which cache, warm with what"),
]

BLIND_SPECIFIC: List[str] = [
    "set MaxAuthTries to 3 in /etc/ssh/sshd_config and reload sshd",
    "delete snapshots in /srv/snapshots older than 90 days",
    "raise OLLAMA_MAX_LOADED_MODELS to 2 in /etc/environment",
    "show every process using more than 4 GB of resident memory",
    "restart containerd.service and confirm it reports active",
    "copy /etc/fstab to /srv/backups/fstab-2026-07-27",
    "run nvidia-smi and report the free VRAM in MB",
    "downgrade docker.io to 24.0.7 with apt",
    "tail -f /var/log/nginx/error.log for 30 seconds",
    "create a 2 GB tmpfs mounted at /mnt/scratch",
]


def evaluate(clarifier: object,
             ambiguous: Optional[List[Tuple[str, str]]] = None,
             specific: Optional[List[str]] = None) -> Dict[str, object]:
    """Score a clarifier against the corpus.

    Args:
        clarifier: Anything with a `detect(task) -> list` method.
        ambiguous: Override the ambiguous set, for holdout scoring.
        specific: Override the specific set, for holdout scoring.

    Returns:
        A metrics dict with false negative and false positive rates, plus the
        specific requests each side got wrong.
    """
    amb_set = AMBIGUOUS if ambiguous is None else ambiguous
    spec_set = SPECIFIC if specific is None else specific

    false_negatives: List[Tuple[str, str]] = []
    for request, why in amb_set:
        if not clarifier.detect(request):
            false_negatives.append((request, why))

    false_positives: List[Tuple[str, List[str]]] = []
    for request in spec_set:
        found = clarifier.detect(request)
        if found:
            false_positives.append((request, [a.code for a in found]))

    n_amb, n_spec = len(amb_set), len(spec_set)
    tp = n_amb - len(false_negatives)
    fp = len(false_positives)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / n_amb if n_amb else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return {
        "ambiguous_total": n_amb,
        "specific_total": n_spec,
        "false_negatives": false_negatives,
        "false_positives": false_positives,
        "fn_rate": round(len(false_negatives) / n_amb, 4) if n_amb else 0.0,
        "fp_rate": round(fp / n_spec, 4) if n_spec else 0.0,
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
    }


def report(name: str, metrics: Dict[str, object]) -> None:
    """Print a readable scorecard."""
    print(f"\n{name}")
    print("-" * len(name))
    print(f"  ambiguous caught : {metrics['ambiguous_total'] - len(metrics['false_negatives'])}"
          f"/{metrics['ambiguous_total']}  (recall {metrics['recall']:.0%})")
    print(f"  specific cleared : {metrics['specific_total'] - len(metrics['false_positives'])}"
          f"/{metrics['specific_total']}  (fp rate {metrics['fp_rate']:.0%})")
    print(f"  precision {metrics['precision']:.0%}   f1 {metrics['f1']:.2f}")

    if metrics["false_negatives"]:
        print(f"\n  FALSE NEGATIVES ({len(metrics['false_negatives'])}), "
              "ambiguous requests waved through:")
        for request, why in metrics["false_negatives"]:
            print(f"    {request!r:52} ({why})")
    if metrics["false_positives"]:
        print(f"\n  FALSE POSITIVES ({len(metrics['false_positives'])}), "
              "specific requests blocked:")
        for request, codes in metrics["false_positives"]:
            print(f"    {request!r:52} {codes}")


def main() -> None:
    """Score every available clarifier against all three corpora."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from core_behaviors import AskClarifyingQuestionsBehavior

    v1 = evaluate(AskClarifyingQuestionsBehavior())
    report("Phase 1 clarifier (regex table)", v1)

    try:
        from core_behaviors_v4 import StructuralClarifier
    except ImportError:
        print("\n(core_behaviors_v4 not present, skipping the v4 comparison)")
        return

    v4 = evaluate(StructuralClarifier())
    report("Phase 4 clarifier (structural)", v4)

    print("\nDELTA (tuning corpus)")
    print("---------------------")
    print(f"  false negative rate {v1['fn_rate']:.0%} -> {v4['fn_rate']:.0%}")
    print(f"  false positive rate {v1['fp_rate']:.0%} -> {v4['fp_rate']:.0%}")
    print(f"  f1                  {v1['f1']:.2f} -> {v4['f1']:.2f}")

    h1 = evaluate(AskClarifyingQuestionsBehavior(),
                  HOLDOUT_AMBIGUOUS, HOLDOUT_SPECIFIC)
    h4 = evaluate(StructuralClarifier(), HOLDOUT_AMBIGUOUS, HOLDOUT_SPECIFIC)
    report("Phase 1 clarifier, HOLDOUT", h1)
    report("Phase 4 clarifier, HOLDOUT", h4)

    print("\nDELTA (holdout, used once to diagnose the closed-verb-list defect)")
    print("-----------------------------------------------------------------")
    print(f"  false negative rate {h1['fn_rate']:.0%} -> {h4['fn_rate']:.0%}")
    print(f"  false positive rate {h1['fp_rate']:.0%} -> {h4['fp_rate']:.0%}")
    print(f"  f1                  {h1['f1']:.2f} -> {h4['f1']:.2f}")

    b1 = evaluate(AskClarifyingQuestionsBehavior(), BLIND_AMBIGUOUS, BLIND_SPECIFIC)
    b4 = evaluate(StructuralClarifier(), BLIND_AMBIGUOUS, BLIND_SPECIFIC)
    report("Phase 1 clarifier, BLIND", b1)
    report("Phase 4 clarifier, BLIND", b4)

    print("\nDELTA (blind set, never optimised against, the honest number)")
    print("-------------------------------------------------------------")
    print(f"  false negative rate {b1['fn_rate']:.0%} -> {b4['fn_rate']:.0%}")
    print(f"  false positive rate {b1['fp_rate']:.0%} -> {b4['fp_rate']:.0%}")
    print(f"  f1                  {b1['f1']:.2f} -> {b4['f1']:.2f}")


if __name__ == "__main__":
    main()
