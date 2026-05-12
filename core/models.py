from django.db import models
import os

class SiteBatch(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    file = models.FileField(upload_to='uploads/')
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    total_sites = models.IntegerField(default=0)
    completed_sites = models.IntegerField(default=0)
    failed_sites = models.IntegerField(default=0)
    current_action = models.CharField(max_length=255, blank=True, null=True)

    @property
    def processed_sites(self):
        """Finished rows (success + failed) — drives progress bar consistency."""
        return (self.completed_sites or 0) + (self.failed_sites or 0)

    @property
    def progress_percentage(self):
        if self.total_sites == 0:
            return 0
        return int((self.processed_sites / self.total_sites) * 100)

    def __str__(self):
        return f"Batch {self.id} - {self.created_at}"

class SiteEntry(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]
    batch = models.ForeignKey(SiteBatch, on_delete=models.CASCADE, related_name='entries')
    title = models.CharField(max_length=255)
    keyword = models.CharField(max_length=255, blank=True, null=True)
    content = models.TextField()
    slug = models.SlugField(max_length=255)
    outbound_link = models.URLField(blank=True, null=True)
    image_url = models.URLField(blank=True, null=True)
    published_url = models.URLField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(blank=True, null=True)
    retry_count = models.IntegerField(default=0)

    def __str__(self):
        return self.title
