from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from content_reviews.models import ContentSubmission, ContentVersion
from workspaces.models import WorkspaceMembership
from workspaces.permissions import workspace_member_required

from .forms import AIAnalysisConsentForm
from .models import AIAnalysisRun
from .services import AIAnalysisRejected, ai_analysis_mode, run_ai_analysis


@require_POST
@workspace_member_required
def analyze_version(request, submission_id, version_number):
    submission = get_object_or_404(
        ContentSubmission.objects.filter(workspace_id=request.workspace.pk), pk=submission_id
    )
    if (
        request.workspace_membership.role != WorkspaceMembership.Role.CREATOR
        or submission.creator_id != request.user.pk
    ):
        raise PermissionDenied("Only the creator may request AI analysis for their own content.")
    version = get_object_or_404(submission.versions, version_number=version_number)
    demo_mode = ai_analysis_mode() == "demo"
    form = AIAnalysisConsentForm(request.POST, demo_mode=demo_mode)
    if not form.is_valid():
        for error_list in form.errors.values():
            for error in error_list:
                messages.error(request, error)
        return redirect(
            "app:content_reviews:version_detail",
            submission_id=submission.pk,
            version_number=version.version_number,
        )
    try:
        run = run_ai_analysis(
            submission_id=submission.pk,
            version_number=version.version_number,
            workspace=request.workspace,
            actor=request.user,
            idempotency_key=form.cleaned_data["idempotency_key"],
            consent=form.cleaned_data["confirm_demo_simulation" if demo_mode else "consent_external_processing"],
        )
    except ContentSubmission.DoesNotExist:
        raise Http404("This submission is not available in the current workspace.")
    except ContentVersion.DoesNotExist:
        raise Http404("This content version is not available.")
    except AIAnalysisRejected as exc:
        messages.error(request, exc.public_message)
        return redirect(
            "app:content_reviews:version_detail",
            submission_id=submission.pk,
            version_number=version.version_number,
        )
    except ValidationError as exc:
        messages.error(request, "AI analysis could not be started: " + " ".join(exc.messages))
        return redirect(
            "app:content_reviews:version_detail",
            submission_id=submission.pk,
            version_number=version.version_number,
        )
    if run.status == AIAnalysisRun.Status.COMPLETE:
        if run.provider == "demo":
            messages.success(request, "Demo AI analysis is ready. This is a simulated response; no external AI provider was contacted. It is separate from deterministic findings and does not affect approval.")
        else:
            messages.success(request, "AI advisory analysis is ready. It is separate from deterministic findings and does not affect approval.")
    elif run.status == AIAnalysisRun.Status.RUNNING:
        messages.info(request, "An analysis for this content and policy is already underway. Its saved result will appear here when processing finishes.")
    else:
        messages.error(
            request,
            f"AI analysis did not complete ({run.get_error_category_display()}). Deterministic review results and approvals are unchanged.",
        )
    return redirect(
        "app:content_reviews:version_detail",
        submission_id=submission.pk,
        version_number=version.version_number,
    )
