import logging

from actstream import action as actstream_action
from django.conf import settings

from policyengine.utils import ActionKind

logger = logging.getLogger(__name__)
db_logger = logging.getLogger("db")


class EvaluationLogAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        kwargs["extra"] = self.extra
        return (msg, kwargs)


class EvaluationContext:
    """
    Class to hold all variables available in a policy evaluation.
    All attributes on this class are in scope and can be used by the policy author.

    Attributes:
        proposal (Proposal): The proposal representing this evaluation.
        action (BaseAction): The action that triggered this policy evaluation.
        policy (Policy): The policy being evaluated.
        slack (SlackCommunity)
        discord (DiscordCommunity)
        discourse (DiscourseCommunity)
        reddit (RedditCommunity)
        github (GithubCommunity)
        metagov (Metagov): Metagov library for performing enabled actions and processes.
        logger (logging.Logger): Logger that will log messages to the PolicyKit web interface.

    """

    def __init__(self, proposal):
        self.action = proposal.action
        self.policy = proposal.policy
        self.proposal = proposal
        self.logger = EvaluationLogAdapter(
            db_logger, {"community": proposal.action.community.community, "proposal": proposal}
        )

        from policyengine.models import Community, CommunityPlatform

        parent_community: Community = self.action.community.community

        # Make all CommunityPlatforms available in the evaluation context
        for comm in CommunityPlatform.objects.filter(community=parent_community):
            setattr(self, comm.platform, comm)

        if settings.METAGOV_ENABLED:
            from integrations.metagov.library import Metagov

            self.metagov = Metagov(proposal)


class PolicyEngineError(Exception):
    """Base class for exceptions raised from the policy engine"""

    pass


class PolicyCodeError(PolicyEngineError):
    """Raised when an exception is raised in a policy"""

    def __init__(self, step, message):
        self.step = step
        self.message = message
        super().__init__(self.message)


class PolicyDoesNotExist(PolicyEngineError):
    """Raised when trying to evaluate a Proposal where the policy has been deleted"""

    pass


class PolicyIsNotActive(PolicyEngineError):
    """Raised when trying to evaluate a Proposal where the policy has been marked inactive"""

    pass


class PolicyDoesNotPassFilter(PolicyEngineError):
    """Raised when trying to evaluate a Proposal where the action no longer passes the policy's filter step"""

    pass


def govern_action(action):
    """
    Called the FIRST TIME that an action is evaluated.
    - If the initiator has "can execute" permission, execute the action and mark it as "passed."
    - Otherwise, choose a Policy to evaluate.
    - Create a Proposal and run it.
    """
    from policyengine.models import (
        ConstitutionAction,
        ConstitutionActionBundle,
        PlatformAction,
        PlatformActionBundle,
        Proposal,
    )

    # if they have execute permission, skip all policies
    if action.initiator.has_perm(f"{action._meta.app_label}.can_execute_{action.action_type}"):
        action.execute()
        # No `Proposal` is created because we don't evaluate it
    else:
        eligible_policies = None
        if isinstance(action, PlatformAction) or isinstance(action, PlatformActionBundle):
            eligible_policies = action.community.get_platform_policies().filter(is_active=True)
        elif isinstance(action, ConstitutionAction) or isinstance(action, ConstitutionActionBundle):
            eligible_policies = action.community.get_constitution_policies().filter(is_active=True)
        else:
            raise Exception("govern_action: unrecognized action")

        existing_proposals = Proposal.objects.filter(action=action)
        if existing_proposals:
            logger.warn(f"There are already {existing_proposals.count()} proposals for action {action}")

        while eligible_policies.exists():
            proposal = choose_policy(action, eligible_policies)
            if not proposal:
                # This means that the action didn't pass the filter for ANY policies.
                return None

            # Run the proposal
            try:
                evaluate_proposal(proposal, is_first_evaluation=True)
            except Exception as e:
                eligible_policies = eligible_policies.exclude(pk=proposal.policy.pk)
                logger.debug(f"{proposal} raised a exception '{e}', choosing a different policy...")
                proposal.delete()
                pass
            else:
                return proposal


def choose_policy(action, policies):
    from policyengine.models import Policy, Proposal

    for policy in policies:
        proposal = Proposal.objects.create(policy=policy, action=action, status=Proposal.PROPOSED)
        context = EvaluationContext(proposal)
        try:
            passed_filter = exec_code_block(policy.filter, context, Policy.FILTER)
        except Exception as e:
            # Log unhandled exception to the db, so policy author can view it in the UI.
            context.logger.error(f"Exception in 'filter': {str(e)}")
            proposal.delete()
            # If there was an exception raised in 'filter', treat it as if the action didn't pass this policy's filter.
            continue

        if passed_filter:
            logger.debug(f"For action '{action}', choosing policy '{policy}'")
            return proposal

        proposal.delete()

    logger.debug(f"For action {action}, no matching policy found!")


def delete_and_rerun(proposal):
    """
    Delete the proposal and re-run govern_action for the relevant action.
    Called when the proposal becomes invalid, because the policy was deleted or is no longer relevant.
    """
    action = proposal.action
    proposal.delete()
    new_evaluation = govern_action(action)
    return new_evaluation


def evaluate_proposal(proposal, is_first_evaluation=False):
    """
    Evaluate policy for given action. This can be run repeatedly to check proposed actions.
    """

    if not proposal.policy:
        # This could happen if the Policy has been deleted since the first proposal.
        raise PolicyDoesNotExist

    if not proposal.policy.is_active:
        raise PolicyIsNotActive

    context = EvaluationContext(proposal)
    try:
        return evaluate_proposal_inner(context, is_first_evaluation)
    except PolicyDoesNotPassFilter:
        # The policy changed so that the action no longer passes the 'filter' step
        raise
    except PolicyCodeError as e:
        # Log policy code exception to the db, so policy author can view it in the UI.
        context.logger.error(f"Exception raised in '{e.step}' block: {e.message}")
        raise
    except Exception as e:
        # Log unhandled exception to the db, so policy author can view it in the UI.
        context.logger.error("Unhandled exception: " + str(e))
        raise


def evaluate_proposal_inner(context: EvaluationContext, is_first_evaluation: bool):
    from policyengine.models import Policy, Proposal

    policy = context.policy
    action = context.action
    proposal = context.proposal

    if not exec_code_block(policy.filter, context, Policy.FILTER):
        raise PolicyDoesNotPassFilter

    # If policy is being evaluated for the first time, initialize it
    if is_first_evaluation:
        # run "initialize" block of policy
        exec_code_block(policy.initialize, context, Policy.INITIALIZE)

    # Run "check" block of policy
    check_result = exec_code_block(policy.check, context, Policy.CHECK)
    check_result = sanitize_check_result(check_result)
    context.logger.debug(f"Check returned '{check_result}'")

    if check_result == Proposal.PASSED:
        # run "pass" block of policy
        exec_code_block(policy.success, context, Policy.SUCCESS)
        # mark proposal as 'passed'
        proposal.pass_evaluation()
        assert proposal.status == Proposal.PASSED

        # EXECUTE the action if....
        # it is a PlatformAction that was proposed in the PolicyKit UI
        if action.action_kind == ActionKind.PLATFORM and not action.community_origin:
            action.execute()
        # it is a constitution action
        elif action.action_kind == ActionKind.CONSTITUTION:
            action.execute()

        if settings.METAGOV_ENABLED:
            # Close pending process if exists (does nothing if process was already closed)
            context.metagov.close_process()

    if check_result == Proposal.FAILED:
        # run "fail" block of policy
        exec_code_block(policy.fail, context, Policy.FAIL)
        # mark proposal as 'failed'
        proposal.fail_evaluation()
        assert proposal.status == Proposal.FAILED

        if settings.METAGOV_ENABLED:
            # Close pending process if exists (does nothing if process was already closed)
            context.metagov.close_process()

    # Revert the action if necessary
    should_revert = (
        is_first_evaluation
        and check_result in [Proposal.PROPOSED, Proposal.FAILED]
        and action.action_kind == ActionKind.PLATFORM
        and action.community_origin
    )

    if should_revert:
        context.logger.debug(f"Reverting action")
        action.revert()

    # If this action is moving into pending state for the first time, run the Notify block (to start a vote, maybe)
    if check_result == Proposal.PROPOSED and is_first_evaluation:
        actstream_action.send(
            action, verb="was proposed", community_id=action.community.id, action_codename=action.action_type
        )
        # Run "notify" block of policy
        context.logger.debug(f"Notifying")
        exec_code_block(policy.notify, context, Policy.NOTIFY)

    return True


def exec_code_block(code_string: str, context: EvaluationContext, step_name="unknown"):
    wrapper_start = "def func():\r\n"
    lines = ["  " + item for item in code_string.splitlines()]
    wrapper_end = "\r\nresult = func()"
    code = wrapper_start + "\r\n".join(lines) + wrapper_end

    try:
        return exec_code(code, context)
    except Exception as e:
        logger.exception(f"Got exception in exec_code {step_name} step:")
        raise PolicyCodeError(step=step_name, message=str(e))


def exec_code(code, context: EvaluationContext):
    PASSED, FAILED, PROPOSED = "passed", "failed", "proposed"
    _locals = locals().copy()
    # Add all attributes on EvaluationContext to scope
    _locals.update(context.__dict__)
    # Remove some variables from scope
    _locals.pop("code")
    _locals.pop("context")

    exec(code, _locals, _locals)
    return _locals.get("result")


def sanitize_check_result(res):
    from policyengine.models import Proposal

    if res in [Proposal.PROPOSED, Proposal.PASSED, Proposal.FAILED]:
        return res
    return Proposal.PROPOSED