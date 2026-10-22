from django.http import JsonResponse
from django.shortcuts import render

def landing(request):
    return render(request, "core/landing.html")

def health(request):
    """Process health only; this endpoint does not claim database health."""
    return JsonResponse({"status": "ok"})
