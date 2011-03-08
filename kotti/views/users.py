"""User management screens
"""

import re
from urllib import urlencode

from pyramid.httpexceptions import HTTPFound
from pyramid.exceptions import Forbidden
from pyramid.view import is_response
import colander
import deform
from deform import Form
from deform import Button
from deform.widget import AutocompleteInputWidget
from deform.widget import CheckedPasswordWidget
from deform.widget import CheckboxChoiceWidget

from kotti.security import USER_MANAGEMENT_ROLES
from kotti.security import ROLES
from kotti.security import SHARING_ROLES
from kotti.security import get_principals
from kotti.security import map_principals_with_local_roles
from kotti.security import list_groups_raw
from kotti.security import list_groups_ext
from kotti.security import set_groups
from kotti.views.site_setup import CONTROL_PANEL_LINKS
from kotti.views.util import TemplateAPIEdit
from kotti.views.util import is_root
from kotti.views.util import FormController

def roles_form_handler(context, request, available_role_names, groups_lister):
    changed = []
    
    if 'apply' in request.params:
        p_to_r = {}
        for name in request.params:
            if name.startswith('orig-role::'):
                # orig-role::* is hidden checkboxes that allow us to
                # see what checkboxes were in the form originally
                token, principal_name, role_name = name.split('::')
                if role_name not in available_role_names:
                    raise Forbidden()
                new_value = bool(request.params.get(
                    'role::%s::%s' % (principal_name, role_name)))
                if principal_name not in p_to_r:
                    p_to_r[principal_name] = set()
                if new_value:
                    p_to_r[principal_name].add(role_name)

        for principal_name, new_role_names in p_to_r.items():
            # We have to be careful with roles that aren't mutable here:
            orig_role_names = set(groups_lister(principal_name, context))
            orig_sharing_role_names = set(
                r for r in orig_role_names if r in available_role_names)
            if new_role_names != orig_sharing_role_names:
                final_role_names = orig_role_names - set(available_role_names)
                final_role_names |= new_role_names
                changed.append((principal_name, context, final_role_names))

        if changed:
            request.session.flash(u'Your changes have been applied.', 'success')
        else:
            request.session.flash(u'No changes made.', 'info')

    return changed

def search_principals(request, context=None, ignore=None, extra=()):
    flash = request.session.flash
    principals = get_principals()

    if ignore is None:
        ignore = set()

    entries = []
    for principal_name in extra:
        if principal_name not in ignore:
            p = principals[principal_name]
            entries.append((p, list_groups_ext(principal_name, context)))
            ignore.add(principal_name)

    if 'search' in request.params:
        query = '*%s*' % request.params['query']
        found = False
        for p in principals.search(name=query, title=query, email=query):
            found = True
            if p.name not in ignore:
                entries.append((p, list_groups_ext(p.name, context)))
        if not found:
            flash(u'No users or groups found.', 'info')

    return entries

def share_node(context, request):
    # Allow roles_form_handler to do processing on 'apply':
    changed = roles_form_handler(
        context, request, SHARING_ROLES, list_groups_raw)
    if changed:
        for (principal_name, context, groups) in changed:
            set_groups(principal_name, context, groups)
        return HTTPFound(location=request.url)

    existing = map_principals_with_local_roles(context)
    def with_roles(entry):
        all_groups = entry[1][0]
        return [g for g in all_groups if g.startswith('role:')]
    existing = filter(with_roles, existing)
    seen = set([entry[0].name for entry in existing])

    # Allow search to take place and add some entries:
    entries = existing + search_principals(request, context, ignore=seen)
    available_roles = [ROLES[role_name] for role_name in SHARING_ROLES]

    return {
        'api': TemplateAPIEdit(context, request),
        'entries': entries,
        'available_roles': available_roles,
        }

def name_pattern_validator(node, value):
    """
      >>> name_pattern_validator(None, u'bob')
      >>> name_pattern_validator(None, u'b ob')
      Traceback (most recent call last):
      Invalid: <unprintable Invalid object>
      >>> name_pattern_validator(None, u'b:ob')
      Traceback (most recent call last):
      Invalid: <unprintable Invalid object>
    """
    valid_pattern = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
    if not valid_pattern.match(value):
        raise colander.Invalid(node, u"Invalid value")

def name_new_validator(node, value):
    if get_principals().get(value.lower()) is not None:
        raise colander.Invalid(node, u"A user with that name already exists.")

def roleset_validator(node, value):
    oneof = colander.OneOf(USER_MANAGEMENT_ROLES)
    [oneof(node, item) for item in value]

def group_validator(node, value):
    """
      >>> group_validator(None, u'this-group-never-exists')
      Traceback (most recent call last):
      Invalid: <unprintable Invalid object>
    """
    principals = get_principals()
    if principals.get('group:' + value) is None:
        raise colander.Invalid(node, u"No such group: %s" % value)

class Groups(colander.SequenceSchema):
    group = colander.SchemaNode(
        colander.String(),
        validator=group_validator,
        widget=AutocompleteInputWidget(),
        )

class PrincipalSchema(colander.MappingSchema):
    name = colander.SchemaNode(
        colander.String(),
        validator=colander.All(name_pattern_validator, name_new_validator),
        )
    title = colander.SchemaNode(colander.String())
    email = colander.SchemaNode(colander.String())
    password = colander.SchemaNode(
        colander.String(),
        validator=colander.Length(min=5),
        widget=CheckedPasswordWidget(),
        )
    active = colander.SchemaNode(colander.Boolean())
    roles = colander.SchemaNode(
        deform.Set(allow_empty=True),
        validator=roleset_validator,
        missing=[],
        title=u"Global roles",
        widget=CheckboxChoiceWidget(),
        )
    groups = Groups(missing=[])

def principal_add_schema(base=PrincipalSchema()):
    principals = get_principals()
    all_groups = []
    for p in principals.search(name=u'group:*'):
        all_groups.append(p.name.split(u'group:')[1])

    schema = base.clone()
    del schema['active']
    schema['groups']['group'].widget.values = all_groups
    schema['roles'].widget.values = [
        (n, ROLES[n].title) for n in USER_MANAGEMENT_ROLES]
    return schema

def user_schema(base=PrincipalSchema()):
    schema = principal_add_schema(base)
    schema['password'].description = (
        u"Leave this empty and provide an email address below "
        u"to send the user an email to set their own password."
        )
    schema['title'].title = u"Full name"
    return schema

def group_schema(base=PrincipalSchema()):
    schema = principal_add_schema(base)
    del schema['password']
    schema['email'].missing = None
    return schema

def user_management(context, request):
    api = TemplateAPIEdit(
        context, request,
        page_title=u"User Management - %s" % context.title,
        cp_links=CONTROL_PANEL_LINKS,
        )
    principals = get_principals()

    def groups_lister(principal_name, context):
        return principals[principal_name].groups

    # Handling the user/roles matrix:
    changed = roles_form_handler(
        context, request, USER_MANAGEMENT_ROLES, groups_lister)
    if changed:
        changed_names = []
        for (principal_name, context, groups) in changed:
            principal = principals[principal_name]
            principal.groups = list(groups)
            changed_names.append(principal_name)
        location = request.url.split('?')[0] + '?' + urlencode(
            {'extra': ','.join(changed_names)})
        return HTTPFound(location=location)

    extra = request.params.get('extra') or ()
    if extra:
        extra = extra.split(',')
    search_entries = search_principals(request, extra=extra)
    available_roles = [ROLES[role_name] for role_name in USER_MANAGEMENT_ROLES]

    # Add forms:

    # These are callbacks that we pass to the form controller.  They
    # take control as soon as the data validates and are responsible
    # for adding the actual principals and redirect:
    def add_user(context, request, appstruct):
        groups = appstruct['groups']
        all_groups = [
            u'group:%s' % g for g in groups] + list(appstruct['roles'])
        del appstruct['roles']
        appstruct['groups'] = all_groups
        name = appstruct['name'].lower()
        get_principals()[name] = appstruct
        request.session.flash(u'%s added.' % appstruct['title'], 'success')
        location = request.url.split('?')[0] + '?' + urlencode({'extra': name})
        return HTTPFound(location=location)
        
    def add_group(context, request, appstruct):
        appstruct['name'] = u'group:%s' % appstruct['name']
        return add_user(context, request, appstruct)

    # The actual add forms:
    uschema = user_schema()
    user_form = Form(
        uschema,
        buttons=(Button('add-user', u'Add User'), 'cancel'),
        )
    user_fc = FormController(
        user_form,
        add=True,
        add_item=add_user,
        post_key='add-user',
        )
    user_addform = user_fc(context, request)
    if is_response(user_addform):
        return user_addform

    gschema = group_schema()
    group_form = Form(
        gschema,
        buttons=(Button('add-group', u'Add Group'), 'cancel'),
        )
    group_fc = FormController(
        group_form,
        add=True,
        add_item=add_group,
        post_key='add-group',
        )
    group_addform = group_fc(context, request)
    if is_response(group_addform):
        return group_addform

    return {
        'api': api,
        'entries': search_entries,
        'available_roles': available_roles,
        'user_addform': user_addform,
        'group_addform': group_addform,
        }

def includeme(config):
    config.add_view(
        share_node,
        name='share',
        permission='manage',
        renderer='../templates/edit/share.pt',
        )

    config.add_view(
        user_management,
        name='setup-users',
        permission='admin',
        custom_predicates=(is_root,),
        renderer='../templates/site-setup/users.pt',
        )
