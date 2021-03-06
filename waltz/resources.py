import re
import os
import difflib
from glob import glob
import gzip
import json
from collections import OrderedDict
from pprint import pprint
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import walk_tree, preserve_literal

from waltz.html_markdown_utilities import h2m, m2h

from waltz.yaml_setup import yaml
from waltz.canvas_tools import get, put, post, get_setting
from waltz.canvas_tools import from_canvas_date, to_canvas_date

from waltz.utilities import (ensure_dir, make_safe_filename, indent4,
                             make_datetime_filename, log,
                             to_friendly_date, from_friendly_date)

class WaltzException(Exception):
    pass

class WaltzNoResourceFound(WaltzException):
    pass

class ResourceID:
    def __init__(self, course, raw):
        self.course = course
        self.raw = raw
        self.category, self.command, self.name, self.resource_type = ResourceID._parse_type(raw)
        self._get_canvas_data()
        self._get_disk_path()
    
    @staticmethod
    def _parse_type(raw):
        category, action = raw.split("/", 1)
        category = category.lower()
        if category not in RESOURCE_CATEGORIES:
            raise WaltzException("Category {} not found (full Resource ID: {!r})".format(
                category, raw
            ))
        resource_type = RESOURCE_CATEGORIES[category]
        if action == "*":
            return category, "*", None, resource_type
        else:
            command, name = action[0], action[1:]
            return category, command, name, resource_type
    
    def _new_canvas_resource(self):
        potentials = self.resource_type.find_resource_on_canvas(self.course, self.name)
        if not potentials:
            return True
        else:
            raise WaltzException("Resource {} already exists:\n{}".format(
                self.name,
                indent4("\n".join(Resource.get_names_from_json(potentials)))
            ))
    
    def _find_canvas_resource(self):
        potentials = self.resource_type.find_resource_on_canvas(self.course, self.name)
        if not potentials:
            raise WaltzNoResourceFound("No {} resource found for: {}".format(
                self.resource_type.canvas_name, self.raw
            ))
        elif len(potentials) > 1:
            raise WaltzNoResourceFound("Ambiguous {} resource ID: {}\nMatches:\n{}".format(
                self.resource_type.canvas_name, self.raw,
                indent4("\n".join(self.resource_type.get_names_from_json(potentials)))
            ))
        else:
            return potentials[0]
    
    def _get_canvas_resource(self):
        return self.resource_type.get_resource_on_canvas(self.course, self.name)
    
    def _get_canvas_data(self):
        '''
        Looks for the resource on Canvas, either searching for it or looking
        it up directly.
        '''
        if self.command.startswith("+"):
            self.canvas_data = self._new_canvas_resource()
        elif self.command.startswith("?"):
            self.canvas_data = self._find_canvas_resource()
        elif self.command.startswith(":"):
            self.canvas_data = self._get_canvas_resource()
        else:
            raise WaltzException("Unknown command: "+repr(self.command))
        self._parse_canvas_data()
    
    def _parse_canvas_data(self):
        if self.canvas_data is True:
            self.canvas_title = self.name
            self.canvas_id = None
        else:
            self.canvas_title = self.resource_type.identify_title(self.canvas_data)
            self.canvas_id = self.resource_type.identify_id(self.canvas_data)
    
    def _get_disk_path(self):
        extension = self.resource_type.extension
        self.filename = make_safe_filename(self.canvas_title)+extension
        print(self.filename)
        self.is_new, self.path = self.resource_type.find_resource_on_disk(self.course.root_directory, self.filename)


class Course:
    def __init__(self, root_directory, course_name):
        self.root_directory = root_directory
        self.backups = os.path.join(root_directory, '_backups')
        self.templates = os.path.join(root_directory, '_templates')
        self.env = Environment(loader=FileSystemLoader(self.templates))
        self.setup_filters()
        self.course_name = course_name
    
    def setup_filters(self):
        self.env.filters['load_outcome'] = Outcome.load_outcome_by_name(self)
        self.env.filters['make_link'] = self.make_link
        
    def identify_resource_by_name(self, resource_name):
        for category in ['assignments', 'pages']:
            results = get(category, all=True, course=self.course_name,
                            data={'search_term': resource_name})
            if len(results) > 1:
                raise WaltzException("Too many results for: "+resource_name)
            elif results:
                return category, results[0]
        else:
            raise WaltzException("No results for: "+resource_name)
    
    def make_link(self, resource_name):
        resource_type, resource = self.identify_resource_by_name(resource_name)
        return '[{}]({})'.format(resource_name, resource['html_url'])
    
    def render(self, template_name, data):
        template = self.env.get_template(template_name)
        return template.render(**data)
    
    def pull(self, resource_id):
        '''
        Args:
            resource_id (ResourceID): The resource ID to pull from Canvas.
        Returns:
            JSON: The JSON representation of the object straight from canvas.
        '''
        return resource_id.canvas_data
    
    def to_disk(self, resource_id, resource):
        resource_data = resource.to_disk(resource_id)
        walk_tree(resource_data)
        # Make a backup of the local version
        backed_up = self.backup_resource(resource_id, resource_data)
        if backed_up:
            log("Backed up file: ", resource_id.path)
        ensure_dir(resource_id.path)
        if resource_id.path.endswith('.yaml'):
            with open(resource_id.path, 'wb') as out:
                yaml.dump(resource_data, out)
        else:
            with open(resource_id.path, 'w') as out:
                out.write(resource_data)
    
    def from_disk(self, resource_id):
        '''
        Args:
            resource_id (ResourceID): The resource ID to load in.
        Returns:
            Resource: The formatted resource object
        '''
        if not os.path.exists(resource_id.path):
            return None
        if resource_id.path.endswith('.yaml'):
            with open(resource_id.path) as resource_file:
                resource_yaml = yaml.load(resource_file)
        else:
            with open(resource_id.path) as resource_file:
                resource_yaml = resource_file.read()
        return resource_id.resource_type.from_disk(self, resource_yaml, resource_id)
    
    def from_json(self, resource_id, json_data):
        '''`Canvas<ResourceType>` can be converted to `ResourceType`
            @Course.from_json(ResourceID, JSON) -> @ResourceType'''
        return resource_id.resource_type.from_json(self, json_data)
    
    def to_json(self, resource_id, resource):
        return resource.to_json(self, resource_id)
    
    def to_public(self, resource_id, resource):
        return resource.to_public(resource_id)
    
    def push(self, resource_id, json_data):
        if resource_id.canvas_data is True:
            id = None
        else:
            id = resource_id.resource_type.identify_id(resource_id.canvas_data)
        rtype = resource_id.resource_type
        resource_id.canvas_data =  rtype.put_on_canvas(self.course_name, id, json_data)
        resource_id._parse_canvas_data()
    
    def publicize(self, resource_id, public_data):
        walk_tree(public_data)
        path = str(Path(resource_id.path).with_suffix('.public.yaml'))
        ensure_dir(path)
        if path.endswith('.yaml'):
            with open(path, 'wb') as out:
                yaml.dump(public_data, out)
        else:
            with open(path, 'w') as out:
                out.write(public_data)
    
    def backup_json(self, resource_id, json_data):
        resource_path = resource_id.resource_type.identify_filename(resource_id.filename)
        backup_directory = os.path.join(self.backups, resource_path)
        ensure_dir(backup_directory+"/")
        timestamped_filename = make_datetime_filename() + '.json' +'.gz'
        backup_path = os.path.join(backup_directory, timestamped_filename)
        with gzip.open(backup_path, 'wt', encoding="utf-8") as out:
            json.dump(json_data, out)
    
    def backup_resource(self, resource_id, new_version):
        extension = resource_id.resource_type.extension
        resource_path = resource_id.resource_type.identify_filename(resource_id.filename)
        backup_directory = os.path.join(self.backups, resource_path)
        ensure_dir(backup_directory+"/")
        timestamped_filename = make_datetime_filename() + extension +'.gz'
        backup_path = os.path.join(backup_directory, timestamped_filename)
        if not os.path.exists(resource_id.path):
            return False
        with open(resource_id.path, 'r') as original_file:
            contents = original_file.read()
        if contents == new_version:
            return False
        with gzip.open(backup_path, 'wb') as out:
            out.write(contents.encode())
        return True
    
    def backup_bank(self, bank_source):
        backup_directory = os.path.join(self.backups, bank_source)
        ensure_dir(backup_directory+"/")
        timestamped_filename = make_datetime_filename() + '.yaml' +'.gz'
        backup_path = os.path.join(backup_directory, timestamped_filename)
        with open(resource_id.path, 'rb') as original_file:
            contents = original_file.read()
        with gzip.open(backup_path, 'wb') as out:
            out.write(contents)

class Resource:
    title = "Untitled Instance"
    canvas_title_field = 'title'
    canvas_id_field = 'id'
    
    def __init__(self, **kwargs):
        for key, value in list(kwargs.items()):
            setattr(self, key, value)
            del kwargs[key]
        self.unmatched_parameters = kwargs
    
    def to_json(self, course, resource_id):
        raise NotImplementedError("The to_json method has not been implemented.")
    
    def to_disk(self, resource_id):
        raise NotImplementedError("The to_disk method has not been implemented.")
        
    @classmethod
    def from_json(cls, course, json_data):
        raise NotImplementedError("The from_json method has not been implemented.")
    
    @classmethod
    def from_disk(cls, course, resource_data, resource_id):
        raise NotImplementedError("The from_disk method has not been implemented.")
    
    @classmethod
    def _custom_from_disk(cls, yaml_data):
        pass
    
    def extra_push(self, course, resource_id):
        pass
    
    @classmethod
    def extra_pull(cls, course, resource_id):
        pass
    
    @classmethod
    def put_on_canvas(cls, course_name, id, json_data):
        if id is None:
            verb, endpoint = post, cls.canvas_name
        else:
            verb, endpoint = put, "{}/{}".format(cls.canvas_name, id)
        result = verb(endpoint, data=json_data, course=course_name)
        if 'errors' in result:
            raise WaltzException("Errors in Canvas data: "+repr(result))
        return result
    
    @classmethod
    def find_resource_on_canvas(cls, course, resource_name):
        results = get(cls.canvas_name, params={"search_term": resource_name},
                      course=course.course_name, all=True)
        if 'errors' in results:
            raise WaltzException("Errors in Canvas data: "+repr(results))
        return results
    
    @classmethod
    def get_resource_on_canvas(cls, course, resource_name):
        data = get('{}/{}'.format(cls.canvas_name, resource_name),
                   course=course.course_name)
        print(resource_name)
        if 'errors' in data:
            raise WaltzNoResourceFound("Errors in Canvas data: "+repr(data))
        return data
    
    @classmethod
    def find_resource_on_disk(cls, root, filename):
        search_path = os.path.join(root, cls.canonical_category, '**', filename)
        potentials = glob(search_path, recursive=True)
        if not potentials:
            return True, os.path.join(root, cls.canonical_category, filename)
        elif len(potentials) == 1:
            return False, potentials[0]
        else:
            raise ValueError("Category {} has two files with same name:\n{}"
                .format(self.canonical_category, '\n'.join(potentials)))
    
    @classmethod
    def identify_filename(cls, filename):
        return os.path.join(cls.canonical_category, filename)
    
    @classmethod
    def identify_title(cls, json_data):
        return json_data[cls.canvas_title_field]
    
    @classmethod
    def identify_id(cls, json_data):
        return json_data[cls.canvas_id_field]
    
    @staticmethod
    def _get_first_field(data, *fields, default="", convert=False):
        for field in fields:
            if field in data and data[field]:
                value = data[field]
                if convert and "html" in field:
                    value = convert(value)
                return value
        return default
    
    @classmethod
    def get_names_from_json(cls, json_data):
        return [cls.identify_title(r) for r in json_data]
        
class HtmlContent(Resource):
    def __init__(self, text, template=None):
        self.template = template
        self.text = text
    def _parse_html(self):
        if self.text is None:
            return ""
        if self.text == "":
            return ""
        markdown = h2m(self.text)
        #markdown = re.sub(r'\n\s*\n', '\n\n', markdown)
        markdown = markdown.strip()+"\n"
        return markdown
    def _generate_html(self):
        return ''
        
class Page(Resource):
    category_names = ["page", "pages"]
    canvas_name = 'pages'
    canonical_category = 'pages'
    canvas_title_field = 'title'
    canvas_id_field = 'url'
    extension = '.md'
    
    @classmethod
    def from_json(cls, course, json_data):
        if 'body' not in json_data:
            data = get('{}/{}'.format(cls.canvas_name, json_data['url']),
                       course=course.course_name)
            json_data['body'] = data['body']
        return cls(**json_data, course=course)
    
    def to_disk(self, resource):
        return h2m(self.body)
    
    @classmethod
    def from_disk(cls, course, resource_data, resource_id):
        # Fix configuration on simpler attributes
        return cls(body=m2h(resource_data), course=course,
                   title=resource_id.canvas_title)
    
    def to_json(self, course, resource_id):
        ''' Suitable for PUT request on API'''
        return {
            'wiki_page[body]': self.body,
            'wiki_page[title]': self.title
        }
        
class Outcome(Resource):
    category_names = ["outcome", "outcomes"]
    canvas_name = 'outcomes'
    canonical_category = 'outcomes'
    canvas_title_field = 'title'
    canvas_id_field = 'id'
    extension = '.yaml'
    CACHE = {}
    
    @staticmethod
    def load_outcome_by_name(course):
        def _wrapped(outcome_name):
            if course.course_name not in Outcome.CACHE:
                Outcome.load_all(course)
            return Outcome.CACHE[course.course_name].get(outcome_name, outcome_name)
        return _wrapped
    
    @staticmethod
    def load_all(course):
        category_folder = os.path.join(course.root_directory,
                                       Outcome.canonical_category, 
                                       '**', '*'+Outcome.extension)
        Outcome.CACHE[course.course_name] = {}
        for bank in glob(category_folder, recursive=True):
            with open(bank) as bank_file:
                outcomes = yaml.load(bank_file)
                for name, outcome in outcomes.items():
                    new_outcome = Outcome.from_disk(course, {'body': outcome}, None)
                    new_outcome.bank_source = os.path.dirname(bank)
                    Outcome.CACHE[course.course_name][name] = new_outcome
    
    @classmethod
    def from_disk(cls, course, resource_data, resource_id):
        return cls(**resource_data, course=course)
        
class Assignment(Resource):
    category_names = ["assignments", "assignment", "a"]
    canvas_name = 'assignments'
    canonical_category = 'assignments'
    canvas_title_field = 'name'
    canvas_id_field = 'id'
    extension = '.yaml'
    
    def to_disk(self, resource_id):
        '''Suitable YAML for yaml.dump'''
        result = CommentedMap()
        result['name'] = self.name
        result['url'] = self.html_url
        result['settings'] = CommentedMap()
        result['settings']['published'] = self.published
        result['settings']['points_possible'] = self.points_possible
        result['settings']['grading_type'] = self.grading_type
        
        result['settings']['submission'] = CommentedMap()
        if hasattr(self, 'allowed_extensions'):
            result['settings']['submission']['extensions'] = self.allowed_extensions
        result['settings']['submission']['submission_types'] = self.submission_types
        
        result['settings']['timing'] = CommentedMap()
        result['settings']['timing']['due_at'] = to_friendly_date(self.due_at)
        result['settings']['timing']['unlock_at'] = to_friendly_date(self.unlock_at)
        result['settings']['timing']['lock_at'] = to_friendly_date(self.lock_at)
        
        result['settings']['secrecy'] = CommentedMap()
        result['settings']['secrecy']['anonymize_students'] = self.anonymize_students
        result['settings']['secrecy']['anonymous_grading'] = self.anonymous_grading
        
        result['description'] = h2m(self.description)
        return result
    
    def to_json(self, course, resource_id):
        ''' Suitable for PUT request on API'''
        return {
            'assignment[notify_of_update]': 'false',
            'assignment[name]': resource_id.canvas_title,
            'assignment[description]': self.description,
            #'assignment[submission_types][]': ','.join(self.submission_types),
            #'assignment[allowed_extensions][]': ','.join(self.submission_types),
            'assignment[points_possible]': self.points_possible,
            'assignment[lock_at]': self.lock_at,
            'assignment[unlock_at]': self.unlock_at,
            'assignment[due_at]': self.due_at,
            'assignment[published]': str(self.published).lower(),
        }
    
    @classmethod
    def from_disk(cls, course, yaml_data, resource_id):
        # Fix configuration on simpler attributes
        yaml_data['description'] = m2h(yaml_data['description'])
        yaml_data['settings'].update(yaml_data['settings'].pop('timing'))
        yaml_data['settings'].update(yaml_data['settings'].pop('secrecy'))
        yaml_data['settings'].update(yaml_data['settings'].pop('submission'))
        yaml_data.update(yaml_data.pop('settings'))
        yaml_data['due_at'] = from_friendly_date(yaml_data['due_at'])
        yaml_data['unlock_at'] = from_friendly_date(yaml_data['unlock_at'])
        yaml_data['lock_at'] = from_friendly_date(yaml_data['lock_at'])
        yaml_data['html_url'] = yaml_data.pop('url')
        return cls(**yaml_data, course=course)
    
    @classmethod
    def from_json(cls, course, json_data):
        return cls(**json_data, course=course)

from waltz.quizzes import *

ALL_RESOURCES = [Quiz, Page, Assignment]
RESOURCE_CATEGORIES = {}
for ResourceType in ALL_RESOURCES:
    for category in ResourceType.category_names:
        RESOURCE_CATEGORIES[category] = ResourceType
