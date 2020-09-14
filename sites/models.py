
from django.db import models
from django.db.models import Count
from django.forms import ValidationError
from django.urls import reverse
from django.utils.text import slugify

from modelcluster.fields import ParentalManyToManyField
from modelcluster.models import ClusterableModel
from wagtail.admin.edit_handlers import FieldPanel
from wagtail.images.edit_handlers import ImageChooserPanel
from wagtail.snippets.models import register_snippet

from wagtailautocomplete.edit_handlers import AutocompletePanel


class ScannedSitesManager(models.Manager):
    def get_queryset(self):
        return super(ScannedSitesManager, self).get_queryset().annotate(
            num_scans=Count('scans')
        ).filter(num_scans__gt=0)


class Site(ClusterableModel):
    name = models.CharField('Name', max_length=255, unique=True)
    slug = models.SlugField('Slug', unique=True, editable=False,
                            allow_unicode=True)

    domain = models.CharField(
        'Domain Name',
        max_length=255,
        unique=True,
        help_text='Specify the domain name without the scheme, '
                  'e.g. "example.com" instead of "https://example.com"')

    twitter_handle = models.CharField(
        'Twitter Handle',
        blank=True,
        max_length=16,
        help_text='Specify the twitter handle starting with "@"'
    )

    added = models.DateTimeField(auto_now_add=True)

    objects = models.Manager()
    scanned = ScannedSitesManager()

    regions = ParentalManyToManyField(
        'Region',
        blank=True,
        related_name='sites',
        help_text='Select which leaderboard you would like this '
                  'news site to appear on'
    )

    panels = [
        FieldPanel('name'),
        FieldPanel('domain'),
        FieldPanel('twitter_handle'),
        AutocompletePanel('regions', target_model='sites.Region'),
    ]

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def clean(self):
        self.slug = slugify(self.name, allow_unicode=True)
        if len(self.slug) == 0:
            raise ValidationError('Slug must not be an empty string')

    def save(self, *args, **kwargs):
        # Calling full_clean in save ensures that the slug will be
        # autogenerated and validated no matter what route the data
        # took to get into the database.
        # https://code.djangoproject.com/ticket/13100
        self.full_clean()
        super(Site, self).save(*args, **kwargs)

    def to_dict(self):
        """Generate a JSON-serializable dict of this object's attributes,
        including the results of the most recent scan."""
        # TODO optimize this (denormalize latest scan into Site?)
        return dict(
            name=self.name,
            domain=self.domain,
            absolute_url=self.get_absolute_url(),
            **self.scans.latest().to_dict()
        )

    def get_absolute_url(self):
        return reverse('sites:site', kwargs={'slug': self.slug})


class Scan(models.Model):
    site = models.ForeignKey(
        Site,
        on_delete=models.CASCADE,
        related_name='scans')

    timestamp = models.DateTimeField(auto_now_add=True)

    # Scan results
    # TODO: If a site isn't live, there may not be much of a point storing the
    # scan. This requirement also increases the complexity of the data model
    # since it means the attributes of the scan results must be nullable.
    live = models.BooleanField()

    # These are nullable because it may not be possible to determine their
    # values (for example, if the site is down at the time of the scan).
    onion_available = models.NullBooleanField()
    valid_https = models.NullBooleanField()
    downgrades_https = models.NullBooleanField()
    defaults_to_https = models.NullBooleanField()

    hsts = models.NullBooleanField()
    hsts_max_age = models.IntegerField(null=True)
    hsts_entire_domain = models.NullBooleanField()
    hsts_preload_ready = models.NullBooleanField()
    hsts_preloaded = models.NullBooleanField()

    score = models.IntegerField(default=0, editable=False)

    # To aid debugging, we store the full stdout and stderr from pshtt.
    pshtt_stdout = models.TextField()
    pshtt_stderr = models.TextField()

    class Meta:
        get_latest_by = 'timestamp'

    def __str__(self):
        return "{} from {:%Y-%m-%d %H:%M}".format(self.site.name,
                                                  self.timestamp)

    def save(self, *args, **kwargs):
        self._score()
        super(Scan, self).save(*args, **kwargs)

    def _score(self):
        """Compute a score between 0-100 for the quality of
         the HTTPS implementation observed by this scan."""
        score = 0
        if self.valid_https:
            if self.downgrades_https:
                score = 30
            else:
                score = 50

            if self.defaults_to_https:
                score = 70

                if self.hsts:
                    score += 4

                # HSTS max-age is specified in seconds
                eighteen_weeks = 18*7*24*60*60
                if self.hsts_max_age and self.hsts_max_age >= eighteen_weeks:
                    score += 4

                if self.hsts_entire_domain:
                    score += 6
                if self.hsts_preload_ready:
                    score += 4
                if self.hsts_preloaded:
                    score += 4
                if self.onion_available:
                    score += 4

        assert 0 <= score <= 100, \
            "score must be between 0 and 100 (inclusive), is: {}".format(score)
        self.score = score

    @property
    def grade(self):
        """Return a letter grade for this scan's score"""
        # TODO: We might consider storing this in the database as well to avoid
        # having to frequntly recompute this value.
        score = self.score
        grade = None

        if score > 95:
            grade = 'A+'
        elif score >= 85:
            grade = 'A'
        elif score >= 80:
            grade = 'A-'
        elif score >= 75:
            grade = 'B+'
        elif score >= 65:
            grade = 'B'
        elif score >= 60:
            grade = 'B-'
        elif score >= 55:
            grade = 'C+'
        elif score >= 45:
            grade = 'C'
        elif score >= 40:
            grade = 'C-'
        elif score >= 35:
            grade = 'D+'
        elif score >= 25:
            grade = 'D'
        elif score >= 20:
            grade = 'D-'
        else:
            grade = 'F'

        # TODO Determining the CSS class name here in the model feels
        # like a violation of MVC, but I want to avoid duplicating this
        # logic in Python. (for the score breakdown pages) and Javascript
        # (for the leaderboard).
        class_name = None
        if score >= 80:
            class_name = 'grade-a'
        elif score >= 60:
            class_name = 'grade-b'
        elif score >= 40:
            class_name = 'grade-c'
        elif score >= 20:
            class_name = 'grade-d'
        elif score >= 0:
            class_name = 'grade-f'

        return dict(grade=grade, class_name=class_name)

    def to_dict(self):
        return dict(
            live=self.live,
            onion_available=self.onion_available,
            valid_https=self.valid_https,
            downgrades_https=self.downgrades_https,
            defaults_to_https=self.defaults_to_https,
            hsts=self.hsts,
            hsts_max_age=self.hsts_max_age,
            hsts_entire_domain=self.hsts_entire_domain,
            hsts_preload_ready=self.hsts_preload_ready,
            hsts_preloaded=self.hsts_preloaded,
            score=self.score,
            grade=self.grade
        )


@register_snippet
class Region(ClusterableModel):
    @classmethod
    def autocomplete_create(kls, value):
        return kls.objects.create(name=value)

    autocomplete_search_field = 'name'

    name = models.CharField(max_length=255, unique=True)
    icon = models.ForeignKey(
        'wagtailimages.Image', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+'
    )
    slug = models.SlugField('Slug', unique=True, editable=False,
                            allow_unicode=True)
    panels = [
        FieldPanel('name'),
        ImageChooserPanel('icon'),
    ]

    def autocomplete_label(self):
        return str(self)

    def __str__(self):
        return self.name

    def clean(self):
        self.slug = slugify(self.name, allow_unicode=True)
        if len(self.slug) == 0:
            raise ValidationError('Slug must not be an empty string')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    class Meta:
        verbose_name_plural = 'site regions'
