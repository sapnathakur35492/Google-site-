from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.http import HttpResponse, JsonResponse
from django.db.models import Sum
from django.contrib import messages
from .models import SiteBatch, SiteEntry
import pandas as pd
import threading
from .automation.google_sites import run_automation


class DashboardView(View):
    def get(self, request):
        batches = SiteBatch.objects.all().order_by('-created_at')

        # Aggregate stats across ALL batches
        agg = batches.aggregate(
            total_completed=Sum('completed_sites'),
            total_failed=Sum('failed_sites'),
        )
        total_sites_created = agg['total_completed'] or 0
        total_failed = agg['total_failed'] or 0
        active_jobs = batches.filter(status='processing').count()

        context = {
            'batches': batches,
            'total_sites_created': total_sites_created,
            'total_failed': total_failed,
            'active_jobs': active_jobs,
        }
        return render(request, 'core/dashboard.html', context)


class UploadBatchView(View):
    def get(self, request):
        return render(request, 'core/upload.html')

    def post(self, request):
        file = request.FILES.get('file')
        if not file:
            messages.error(request, 'No file selected.')
            return redirect('upload')

        # Save batch
        batch = SiteBatch.objects.create(file=file)

        # Parse file
        try:
            # Reset file pointer to beginning (critical for Django uploaded files)
            file.seek(0)

            if file.name.endswith('.csv'):
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)

            # Normalize column names to lowercase for flexible matching
            df.columns = [str(c).strip().lower() for c in df.columns]

            batch.total_sites = len(df)
            batch.save()

            from django.utils.text import slugify

            created_count = 0
            for _, row in df.iterrows():
                # Dynamic column detection (case-insensitive)
                title = str(row.get('title', '') or row.get('site_name', '') or '')
                keyword = str(row.get('keyword', '') or row.get('kw', '') or '')

                # Check multiple potential content columns
                content = str(row.get('content', '') or row.get('article', '') or
                              row.get('description', '') or row.get('body', '') or
                              row.get('full_content', '') or '')

                # Skip rows with no meaningful data
                if not title.strip() and not keyword.strip() and not content.strip():
                    continue

                # Clean NaN strings early
                if title.strip().lower() == 'nan':
                    title = ''
                if keyword.strip().lower() == 'nan':
                    keyword = ''
                if content.strip().lower() == 'nan':
                    content = ''

                # Use title if available, otherwise keyword
                if not title.strip():
                    title = keyword

                # Slug: sequential pattern dhr-001, dhr-002...
                # Check if 'slug' column exists and has value
                slug_val = row.get('slug', '')
                if not pd.isna(slug_val) and str(slug_val).strip() and str(slug_val).lower() != 'nan':
                    slug = slugify(str(slug_val))[:80]
                else:
                    # Use sequential counter with 'dhr-' prefix as requested
                    slug = f"dhr-{str(created_count + 1).zfill(3)}"

                # Handle optional columns dynamically
                outbound_link = row.get('outbound_link', None)
                image_url = row.get('image_url', None)

                # Clean NaN values
                if pd.isna(outbound_link): outbound_link = None
                if pd.isna(image_url): image_url = None

                SiteEntry.objects.create(
                    batch=batch,
                    title=title,
                    keyword=keyword,
                    content=content,
                    slug=slug,
                    outbound_link=outbound_link,
                    image_url=image_url
                )
                created_count += 1

            batch.total_sites = created_count
            batch.save()

            messages.success(
                request,
                f'Successfully imported {created_count} site entries from {file.name}. Ready to start automation!'
            )
        except Exception as e:
            batch.status = 'failed'
            batch.save()
            messages.error(request, f'Failed to process file: {str(e)}')
            return redirect('dashboard')

        return redirect('dashboard')


class BatchDetailView(View):
    def get(self, request, batch_id):
        batch = get_object_or_404(SiteBatch, id=batch_id)
        entries = batch.entries.all()
        return render(request, 'core/batch_detail.html', {'batch': batch, 'entries': entries})


class StartAutomationView(View):
    def post(self, request, batch_id):
        batch = get_object_or_404(SiteBatch, id=batch_id)

        # Prevent running two batches at the same time (same browser session)
        already_running = SiteBatch.objects.filter(status='processing').exclude(id=batch_id).exists()
        if already_running:
            messages.error(request, 'Another batch is already running! Please wait for it to finish.')
            return redirect('batch_detail', batch_id=batch.id)

        if batch.status in ('pending', 'failed', 'completed'):
            batch.status = 'processing'
            batch.completed_sites = 0
            batch.failed_sites = 0
            batch.current_action = 'Queued — starting shortly...'
            batch.save()

            # Reset only pending/failed entries (leave already-succeeded entries alone)
            batch.entries.exclude(status='success').update(
                status='pending',
                published_url=None,
                error_message=None,
            )

            # Run in background
            thread = threading.Thread(target=run_automation, args=(batch.id,), daemon=True)
            thread.start()

        return redirect('batch_detail', batch_id=batch.id)


class ExportBatchView(View):
    def get(self, request, batch_id):
        batch = get_object_or_404(SiteBatch, id=batch_id)
        entries = batch.entries.all()

        df = pd.DataFrame(list(entries.values('title', 'slug', 'status', 'published_url', 'error_message')))

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="batch_{batch_id}_results.csv"'

        df.to_csv(path_or_buf=response, index=False)
        return response


class ResetBatchView(View):
    def post(self, request, batch_id):
        batch = get_object_or_404(SiteBatch, id=batch_id)

        # Reset batch level stats
        batch.status = 'pending'
        batch.completed_sites = 0
        batch.failed_sites = 0
        batch.current_action = None
        batch.save()

        # Reset all entries
        batch.entries.all().update(
            status='pending',
            published_url=None,
            error_message=None
        )

        # Clear existing messages to prevent duplicates
        storage = messages.get_messages(request)
        for _ in storage: pass

        messages.success(request, f'Batch #{batch.id} progress cleared! Ready to run again.')
        return redirect('dashboard')


class RetryFailedView(View):
    """Reset only failed entries back to pending, then re-run."""
    def post(self, request, batch_id):
        batch = get_object_or_404(SiteBatch, id=batch_id)

        failed_count = batch.entries.filter(status='failed').count()
        if failed_count == 0:
            messages.info(request, 'No failed entries to retry.')
            return redirect('batch_detail', batch_id=batch.id)

        # Only reset failed entries
        batch.entries.filter(status='failed').update(
            status='pending',
            published_url=None,
            error_message=None,
        )
        batch.failed_sites = 0
        batch.status = 'processing'
        batch.current_action = f'Retrying {failed_count} failed entries...'
        batch.save()

        thread = threading.Thread(target=run_automation, args=(batch.id,), daemon=True)
        thread.start()

        messages.success(request, f'Retrying {failed_count} failed entries...')
        return redirect('batch_detail', batch_id=batch.id)


class BatchStatusAPIView(View):
    """JSON endpoint for real-time polling from the frontend."""
    def get(self, request, batch_id):
        batch = get_object_or_404(SiteBatch, id=batch_id)
        entries = list(batch.entries.all().values(
            'id', 'title', 'slug', 'status', 'published_url', 'error_message'
        ))
        return JsonResponse({
            'batch_id': batch.id,
            'status': batch.status,
            'total_sites': batch.total_sites,
            'completed_sites': batch.completed_sites,
            'failed_sites': batch.failed_sites,
            'processed_sites': batch.processed_sites,
            'progress_percentage': batch.progress_percentage,
            'current_action': batch.current_action or '',
            'entries': entries,
        })


class DeleteBatchView(View):
    """Permanently remove a batch and all its associated site entries."""
    def post(self, request, batch_id):
        batch = get_object_or_404(SiteBatch, id=batch_id)
        
        # Prevent deletion of active jobs to avoid engine crashes
        if batch.status == 'processing':
            messages.error(request, 'Cannot delete a batch that is currently processing!')
            return redirect('dashboard')
            
        batch_id_num = batch.id
        batch.delete()
        
        messages.success(request, f'Batch #{batch_id_num} and all its data have been removed.')
        return redirect('dashboard')
