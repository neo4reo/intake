import importlib
import uuid
from urllib.parse import urljoin
import random
from django.conf import settings
from django.db import models
from pytz import timezone
from django.utils import timezone as timezone_utils
from django.utils.translation import ugettext_lazy as _
from django.contrib.auth.models import User
from django.contrib.postgres.fields import JSONField
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.urlresolvers import reverse

from intake import (
    pdfparser, anonymous_names, notifications, model_fields,
    constants
)

from formation.forms import display_form_selector


def gen_uuid():
    return uuid.uuid4().hex


def get_parser():
    parser = pdfparser.PDFParser()
    parser.PDFPARSER_PATH = getattr(settings, 'PDFPARSER_PATH',
                                    'intake/pdfparser.jar')
    return parser


class CountyManager(models.Manager):

    def get_by_natural_key(self, slug):
        return self.get(slug=slug)


class County(models.Model):
    objects = CountyManager()

    slug = models.SlugField()
    name = models.TextField()
    description = models.TextField()

    def get_receiving_agency(self, submission=None):
        """Returns the appropriate receiving agency
        for this county. Currently there is only one per county,
        but in the future this can be used to make eligibility
        determinations
        """
        # if alameda
        if self.slug == constants.Counties.ALAMEDA:
            # if under 3000 and not owns home
            income = submission.answers.get('monthly_income')
            owns_home = submission.answers.get('owns_home')
            if income < 3000 and not owns_home:
                # return alameda pub def
                return self.organizations.get(
                    slug=constants.Organizations.ALAMEDA_PUBDEF)
            else:
                # return ebclc
                return self.organizations.get(
                    slug=constants.Organizations.EBCLC)
            # return first receiving agency
        return self.organizations.filter(is_receiving_agency=True).first()

    def __str__(self):
        return str(self.name)

    def natural_key(self):
        return (self.slug, )


class FormSubmission(models.Model):

    counties = models.ManyToManyField(County,
                                      related_name="submissions")
    organizations = models.ManyToManyField('user_accounts.Organization',
                                           related_name="submissions")
    answers = JSONField()
    # old_uuid is only used for porting legacy applications
    old_uuid = models.CharField(max_length=34, unique=True,
                                default=gen_uuid)
    anonymous_name = models.CharField(max_length=60,
                                      default=anonymous_names.generate)
    date_received = models.DateTimeField(default=timezone_utils.now)

    class Meta:
        ordering = ['-date_received']

    @classmethod
    def mark_viewed(cls, submissions, user):
        # TODO: doesn't need to be a method here
        logs = ApplicationLogEntry.log_opened(
            [s.id for s in submissions], user)
        # send a slack notification
        notifications.slack_submissions_viewed.send(
            submissions=submissions, user=user)
        return submissions, logs

    @classmethod
    def get_unopened_apps(cls):
        return cls.objects.exclude(
            logs__user__profile__organization__is_receiving_agency=True
        )

    @classmethod
    def get_opened_apps(cls):
        return cls.objects.filter(
            logs__user__profile__organization__is_receiving_agency=True
        ).distinct()

    @classmethod
    def get_permitted_submissions(cls, user, ids=None, related_objects=False):
        query = cls.objects
        if related_objects:
            query = query.prefetch_related(
                'logs__user__profile__organization',
                'counties')
        if ids:
            query = query.filter(pk__in=ids)
        if user.is_staff:
            return query
        county = user.profile.organization.county
        return query.filter(counties=county)

    @classmethod
    def all_plus_related_objects(cls):
        return cls.objects.prefetch_related(
            'logs__user__profile__organization',
            'counties'
        ).all()

    def agency_event_logs(self, event_type):
        '''assumes that self.logs and self.logs.user are prefetched'''
        for log in self.logs.all():
            if log.user and hasattr(log.user, 'profile'):
                if log.user.profile.organization.is_receiving_agency:
                    if log.event_type == event_type:
                        yield log

    def agency_log_time(self, event_type, reduce_func=max):
        return reduce_func((log.time for log in self.agency_event_logs(
            event_type)), default=None)

    def first_opened_by_agency(self):
        return self.agency_log_time(ApplicationLogEntry.OPENED, min)

    def last_opened_by_agency(self):
        return self.agency_log_time(ApplicationLogEntry.OPENED, max)

    def last_processed_by_agency(self):
        return self.agency_log_time(ApplicationLogEntry.PROCESSED, max)

    def get_local_date_received(self, fmt=None, timezone_name='US/Pacific'):
        local_tz = timezone(timezone_name)
        local_datetime = self.date_received.astimezone(local_tz)
        if not fmt:
            return local_datetime
        return local_datetime.strftime(fmt)

    def get_contact_preferences(self):
        if 'contact_preferences' in self.answers:
            return [k for k in self.answers.get('contact_preferences', [])]
        else:
            return [
                k for k in self.answers
                if 'prefers' in k and self.answers[k]
            ]

    def get_nice_contact_preferences(self):
        preferences = [k[8:] for k in self.get_contact_preferences()]
        return [
            nice for key, nice
            in constants.CONTACT_METHOD_CHOICES
            if key in preferences]

    def get_nice_counties(self):
        return self.counties.all().values_list('name', flat=True)

    def get_display_form_for_user(self, user):
        """
        based on user information, get the correct Form class and return it
        instantiated with the data for self
        """
        if not user.is_staff:
            DisplayFormClass = user.profile.get_submission_display_form()
        else:
            DisplayFormClass = display_form_selector.get_combined_form_class(
                counties=[
                    constants.Counties.SAN_FRANCISCO,
                    constants.Counties.CONTRA_COSTA,
                    constants.Counties.ALAMEDA,
                ])
        init_data = dict(
            date_received=self.get_local_date_received(),
            counties=list(self.counties.all().values_list('slug', flat=True))
        )
        init_data.update(self.answers)
        display_form = DisplayFormClass(init_data)
        # initiate parsing
        display_form.is_valid()
        return display_form

    def get_formatted_address(self):
        address = self.answers.get('address', {})
        if not address:
            return ""
        return "{street}\n{city}, {state}\n{zip}".format(
            **self.answers.get('address', {}))

    def get_contact_info(self):
        """Returns a dictionary of contact information structured to be valid for
        intake.fields.ContactInfoJSONField
        """
        info = {}
        for key in self.get_contact_preferences():
            short = key[8:]
            field_name, nice, datum = constants.CONTACT_PREFERENCE_CHECKS[key]
            if short == 'snailmail':
                info[short] = self.get_formatted_address()
            else:
                info[short] = self.answers.get(field_name, '')
        return info

    def send_notification(self, notification, contact_info_key, **context):
        contact_info = self.get_contact_info()
        contact_info_used = {
            contact_info_key: contact_info[contact_info_key]
        }
        notification.send(
            to=[contact_info[contact_info_key]],
            **context
        )
        ApplicationLogEntry.log_confirmation_sent(
            submission_id=self.id, user=None, time=None,
            contact_info=contact_info_used,
            message_sent=notification.render_content_fields(**context)
        )

    def send_confirmation_notifications(self):
        contact_info = self.get_contact_info()
        errors = {}
        counties = self.counties.all()
        county_names = [name + " County" for name in self.get_nice_counties()]
        next_steps = [
            constants.CONFIRMATION_MESSAGES[county.slug]
            for county in counties]
        context = dict(
            staff_name=random.choice(constants.STAFF_NAME_CHOICES),
            name=self.answers['first_name'],
            county_names=county_names,
            organizations=self.organizations.all(),
            next_steps=next_steps)
        notify_map = {
            'email': notifications.email_confirmation,
            'sms': notifications.sms_confirmation
        }
        for key, notification in notify_map.items():
            if key in contact_info:
                try:
                    self.send_notification(
                        notification, key, **context)
                except notifications.FrontAPIError as error:
                    errors[key] = error
        successes = sorted([key for key in contact_info if key not in errors])
        if successes:
            notifications.slack_confirmation_sent.send(
                submission=self,
                methods=successes)
        if errors:
            notifications.slack_confirmation_send_failed.send(
                submission=self,
                errors=errors)
        return self.confirmation_flash_messages(successes, contact_info)

    def confirmation_flash_messages(self, successes, contact_info):
        messages = []
        sent_email_message = _("We've sent you an email at {}")
        sent_sms_message = _("We've sent you a text message at {}")
        for method in successes:
            if method == 'email':
                messages.append(
                    sent_email_message.format(
                        contact_info['email']))
            if method == 'sms':
                messages.append(sent_sms_message.format(contact_info['sms']))
        return messages

    def get_anonymous_display(self):
        return self.anonymous_name

    def __str__(self):
        return self.get_anonymous_display()


class ApplicationLogEntry(models.Model):
    OPENED = 1
    REFERRED = 2
    PROCESSED = 3
    DELETED = 4
    CONFIRMATION_SENT = 5

    EVENT_TYPES = (
        (OPENED, "opened"),
        (REFERRED, "referred"),
        (PROCESSED, "processed"),
        (DELETED, "deleted"),
        (CONFIRMATION_SENT, "sent confirmation"),
    )

    time = models.DateTimeField(default=timezone_utils.now)
    user = models.ForeignKey(User,
                             on_delete=models.SET_NULL, null=True,
                             related_name='application_logs')
    organization = models.ForeignKey(
        'user_accounts.Organization',
        on_delete=models.SET_NULL, null=True,
        related_name='logs')
    submission = models.ForeignKey(FormSubmission,
                                   on_delete=models.SET_NULL, null=True,
                                   related_name='logs')
    event_type = models.PositiveSmallIntegerField(
        choices=EVENT_TYPES)

    class Meta:
        ordering = ['-time']

    @classmethod
    def log_multiple(cls, event_type, submission_ids,
                     user, time=None, organization=None):
        if not time:
            time = timezone_utils.now()
        if event_type in [cls.PROCESSED, cls.OPENED,
                          cls.DELETED] and not organization:
            organization = user.profile.organization
        logs = []
        for submission_id in submission_ids:
            log = cls(
                time=time,
                user=user,
                organization=organization,
                submission_id=submission_id,
                event_type=event_type
            )
            logs.append(log)
        ApplicationLogEntry.objects.bulk_create(logs)
        return logs

    @classmethod
    def log_opened(cls, submission_ids, user, time=None):
        return cls.log_multiple(cls.OPENED, submission_ids, user, time)

    @classmethod
    def log_bundle_opened(cls, bundle, user, time=None):
        sub_ids = bundle.submissions.values_list('pk', flat=True)
        return cls.log_opened(sub_ids, user, time)

    @classmethod
    def log_referred(cls, submission_ids, user, time=None, organization=None):
        return cls.log_multiple(
            cls.REFERRED, submission_ids, user, time, organization)

    @classmethod
    def log_confirmation_sent(cls, submission_id, user,
                              time, contact_info=None, message_sent=''):
        if not time:
            time = timezone_utils.now()
        if not contact_info:
            contact_info = {}
        return ApplicantContactedLogEntry.objects.create(
            submission_id=submission_id,
            user=user,
            event_type=cls.CONFIRMATION_SENT,
            contact_info=contact_info,
            message_sent=message_sent)


class ApplicantContactedLogEntry(ApplicationLogEntry):
    contact_info = model_fields.ContactInfoJSONField(default=dict)
    message_sent = models.TextField(blank=True)


class FillablePDF(models.Model):
    name = models.CharField(max_length=50)
    pdf = models.FileField(upload_to='pdfs/')
    translator = models.TextField()
    organization = models.ForeignKey(
        'user_accounts.Organization',
        on_delete=models.CASCADE,
        related_name='pdfs',
        null=True
    )

    @classmethod
    def get_default_instance(cls):
        return cls.objects.first()

    def get_pdf(self):
        self.pdf.seek(0)
        return self.pdf

    def get_translator(self):
        import_path_parts = self.translator.split('.')
        callable_name = import_path_parts.pop()
        module_path = '.'.join(import_path_parts)
        module = importlib.import_module(module_path)
        return getattr(module, callable_name)

    def get_pdf_fields(self):
        parser = get_parser()
        data = parser.get_field_data(self.get_pdf())
        return data['fields']

    def __str__(self):
        return self.name

    def fill_for_submission(self, submission):
        """Fills out a pdf and saves it to FilledPDF

        used when saving a new submission
        used when retrieving a filled pdf if it doesn't
        """
        filled_pdf_bytes = self.fill(submission)
        pdf_file = SimpleUploadedFile('filled.pdf', filled_pdf_bytes,
                                      content_type='application/pdf')
        pdf = FilledPDF(
            pdf=pdf_file,
            original_pdf=self,
            submission=submission,
        )
        pdf.save()
        return pdf

    def fill(self, *args, **kwargs):
        parser = get_parser()
        translator = self.get_translator()
        return parser.fill_pdf(self.get_pdf(), translator(*args, **kwargs))

    def fill_many(self, data_set, *args, **kwargs):
        if data_set:
            parser = get_parser()
            translator = self.get_translator()
            translated = [translator(d, *args, **kwargs)
                          for d in data_set]
            if len(translated) == 1:
                return parser.fill_pdf(self.get_pdf(), translated[0])
            return parser.fill_many_pdfs(self.get_pdf(), translated)


class FilledPDF(models.Model):
    """A FillablePDF filled with FormSubmission data.
    """
    pdf = models.FileField(upload_to='filled_pdfs/')
    original_pdf = models.ForeignKey(
        FillablePDF,
        on_delete=models.SET_NULL,
        related_name='filled_copies',
        null=True)
    submission = models.ForeignKey(
        FormSubmission,
        on_delete=models.CASCADE,
        related_name='filled_pdfs')


class ApplicationBundle(models.Model):
    submissions = models.ManyToManyField(FormSubmission,
                                         related_name='bundles')
    organization = models.ForeignKey('user_accounts.Organization',
                                     on_delete=models.PROTECT,
                                     related_name='bundles')
    bundled_pdf = models.FileField(upload_to='pdf_bundles/', null=True,
                                   blank=True)

    @classmethod
    def create_with_submissions(cls, **kwargs):
        submissions = kwargs.pop('submissions', [])
        instance = cls(**kwargs)
        instance.save()
        if submissions:
            instance.submissions.add(*submissions)
        return instance

    def get_pdf_bundle_url(self):
        return reverse(
            'intake-app_bundle_detail_pdf',
            kwargs=dict(bundle_id=self.id))

    def get_absolute_url(self):
        return reverse(
            'intake-app_bundle_detail',
            kwargs=dict(bundle_id=self.id))

    def get_external_url(self):
        return urljoin(settings.DEFAULT_HOST, self.get_absolute_url())
